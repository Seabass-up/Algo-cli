"""Gatherer State Machine — priority queue with retry and transactional state.

Borrowed from Windows Search gatherer
(C:\\ProgramData\\Microsoft\\Search\\Data\\Applications\\Windows\\Windows-gather.db,
SystemIndex_Gthr table):
The Windows Search gatherer tracks per-document crawl state including:
  - Priority (0-255, UNSIGNEDBYTE)
  - FailureUpdateAttempts (retry count with exponential backoff)
  - CrawlNumberCrawled (version counter)
  - TransactionFlags (in-progress, committed, rolled-back, retry-pending)
  - LastRequestedRunTime (prevents re-scheduling)

This module implements the same pattern for the harness embedding/indexing
pipeline: files are enqueued with priority, processed in batches, retried on
failure with exponential backoff, and tracked with transactional state.

Pattern: B31 in ALGO.md.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Any, Callable


class TransactionFlags(IntFlag):
    NONE = 0
    IN_PROGRESS = 1
    COMMITTED = 2
    ROLLED_BACK = 4
    RETRY_PENDING = 8


@dataclass
class GathererEntry:
    """A single item in the gatherer queue."""
    path: str
    priority: int = 5  # 0=highest, 255=lowest
    failure_attempts: int = 0
    crawl_number: int = 0
    last_requested_run: float = 0.0
    last_modified: float = 0.0
    transaction_flags: TransactionFlags = TransactionFlags.NONE
    metadata: dict[str, Any] = field(default_factory=dict)

    MAX_ATTEMPTS: int = 3

    def should_retry(self) -> bool:
        return self.failure_attempts < self.MAX_ATTEMPTS

    def next_retry_delay(self) -> float:
        """Exponential backoff: 1s, 2s, 4s, 8s... capped at 300s."""
        return min(2 ** self.failure_attempts, 300)

    def priority_score(self) -> float:
        """Higher = more urgent. Combines static priority + staleness."""
        staleness = time.time() - self.last_modified if self.last_modified else 0
        return (255 - self.priority) * 100 + min(staleness / 3600, 100)

    def is_in_progress(self) -> bool:
        return bool(self.transaction_flags & TransactionFlags.IN_PROGRESS)

    def is_committed(self) -> bool:
        return bool(self.transaction_flags & TransactionFlags.COMMITTED)

    def is_rolled_back(self) -> bool:
        return bool(self.transaction_flags & TransactionFlags.ROLLED_BACK)


class GathererQueue:
    """Priority queue with retry, backoff, and transactional state.

    Usage:
        queue = GathererQueue()
        queue.enqueue(GathererEntry(path="foo.py", priority=3))
        batch = queue.next_batch(batch_size=50)
        for entry in batch:
            queue.mark_in_progress(entry)
            try:
                embed_file(entry.path)
                queue.mark_success(entry)
            except Exception:
                queue.mark_failure(entry)
    """

    def __init__(self) -> None:
        self.entries: dict[str, GathererEntry] = {}

    # --- enqueue / dequeue ------------------------------------------------

    def enqueue(self, entry: GathererEntry) -> None:
        """Add or update an entry in the queue."""
        existing = self.entries.get(entry.path)
        if existing:
            # Preserve retry count and crawl number on re-enqueue
            entry.failure_attempts = existing.failure_attempts
            entry.crawl_number = existing.crawl_number
        self.entries[entry.path] = entry

    def enqueue_many(self, paths: list[str], priority: int = 5) -> None:
        """Bulk enqueue with uniform priority."""
        for path in paths:
            self.enqueue(GathererEntry(path=path, priority=priority))

    # --- batch selection --------------------------------------------------

    def next_batch(
        self, batch_size: int = 50, *, respect_backoff: bool = True,
    ) -> list[GathererEntry]:
        """Get next batch sorted by priority score (descending).

        Filters out:
        - In-progress entries (already being processed)
        - Entries that exceeded max retry attempts (rolled back)
        - Entries whose retry delay hasn't elapsed (unless respect_backoff=False)
        """
        now = time.time()
        eligible: list[GathererEntry] = []
        for entry in self.entries.values():
            if entry.is_in_progress():
                continue
            if entry.is_rolled_back():
                continue
            if not entry.should_retry():
                continue
            # Check retry backoff delay
            if respect_backoff and (entry.transaction_flags & TransactionFlags.RETRY_PENDING):
                if now - entry.last_requested_run < entry.next_retry_delay():
                    continue
            eligible.append(entry)

        eligible.sort(key=lambda e: e.priority_score(), reverse=True)
        return eligible[:batch_size]

    # --- state transitions ------------------------------------------------

    def mark_in_progress(self, entry: GathererEntry) -> None:
        entry.transaction_flags = TransactionFlags.IN_PROGRESS
        entry.last_requested_run = time.time()

    def mark_success(self, entry: GathererEntry) -> None:
        entry.transaction_flags = TransactionFlags.COMMITTED
        entry.failure_attempts = 0
        entry.crawl_number += 1

    def mark_failure(self, entry: GathererEntry) -> None:
        entry.failure_attempts += 1
        if entry.should_retry():
            entry.transaction_flags = TransactionFlags.RETRY_PENDING
        else:
            entry.transaction_flags = TransactionFlags.ROLLED_BACK

    def remove(self, path: str) -> None:
        """Remove a completed entry from the queue."""
        self.entries.pop(path, None)

    # --- queries ----------------------------------------------------------

    def pending_count(self) -> int:
        """Number of entries not yet committed or rolled back."""
        return sum(
            1 for e in self.entries.values()
            if not e.is_committed() and not e.is_rolled_back()
        )

    def committed_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.is_committed())

    def failed_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.is_rolled_back())

    def retry_count(self) -> int:
        return sum(
            1 for e in self.entries.values()
            if e.transaction_flags & TransactionFlags.RETRY_PENDING
        )

    def stats(self) -> dict[str, int]:
        return {
            "total": len(self.entries),
            "pending": self.pending_count(),
            "committed": self.committed_count(),
            "failed": self.failed_count(),
            "retry": self.retry_count(),
        }

    # --- processing loop --------------------------------------------------

    def process(
        self,
        processor: Callable[[GathererEntry], Any],
        *,
        batch_size: int = 50,
        max_rounds: int = 100,
        respect_backoff: bool = True,
    ) -> dict[str, int]:
        """Process the queue until empty or max_rounds reached.

        Args:
            processor: Function that takes a GathererEntry and processes it.
                       Raises on failure.
            batch_size: Max entries per batch.
            max_rounds: Safety limit to prevent infinite loops.
            respect_backoff: If False, skip retry backoff delay (for sync loops).

        Returns:
            Stats dict with committed/failed/retry counts.
        """
        rounds = 0
        while rounds < max_rounds:
            batch = self.next_batch(batch_size, respect_backoff=respect_backoff)
            if not batch:
                break
            for entry in batch:
                self.mark_in_progress(entry)
                try:
                    processor(entry)
                    self.mark_success(entry)
                except Exception:
                    self.mark_failure(entry)
            rounds += 1
        return self.stats()