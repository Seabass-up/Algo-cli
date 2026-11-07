"""Per-turn evidence progress, independent of approval modes and reflex advice.

Tool invocation is not evidence of progress. Keep only bounded, in-memory
fingerprints so identical observations and failures cannot sustain a tool loop.
This is a liveness guard, never an authorization or completion verifier.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any, Iterable


@dataclass(frozen=True)
class ProgressBoundary:
    stop: bool
    reason: str

    def prompt(self, available_tools: Iterable[str] = ()) -> str:
        available = set(available_tools)
        discovery = sorted(available & {"action_search", "available_actions", "harness_search"})
        routes = (
            " First look for another permitted route to the user's objective: a registered typed tool, "
            "app/API adapter, or a different evidence source. A blocked tool does not by itself block the task."
        )
        if discovery:
            routes += " Discover capabilities with " + ", ".join(discovery) + "."
        if "decision_decide" in available:
            routes += " Use decision_decide for a bounded choice among actual available routes when useful."
        return (
            "[Internal evidence-progress boundary] " + self.reason + " "
            + (
                "No more tools are available. Report the task as incomplete: what was actually "
                "established or changed, what remains unverified, and the exact missing capability "
                "or user action needed. Do not claim an unexecuted action succeeded."
                if self.stop else
                routes + " Do not reread unchanged sources or rephrase a denied action. Use a permitted "
                "alternative only if it provides new evidence or advances the task; otherwise "
                "report the blocker now. Further unchanged inspection will end tool execution."
            )
        )


class ToolProgressGuard:
    """Allow new sources/ranges/results, warn on repetition, then stop tools.

    Three stale batches trigger a warning; two more trigger a partial handoff.
    A mostly repeated twelve-result window also catches rotating files with an
    occasional new read. Genuine progress clears the warning. Successful,
    non-deduplicated effects start a fresh evidence epoch for verification.
    """

    def __init__(self, cwd: str) -> None:
        self.cwd = str(cwd)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._recent: deque[bool] = deque(maxlen=12)
        self._blockers: set[str] = set()
        self._batch_count = 0
        self._batch_progress = False
        self._stale_batches = 0
        self._warning_rounds = 0
        self._warned = False

    def _fingerprint(self, name: str, args: dict[str, Any], result: str) -> str:
        normalized = dict(args)
        # A larger requested budget is not new evidence when output is unchanged.
        for key in ("max_chars", "limit"):
            normalized.pop(key, None)
        if name in {"search_files", "harness_search", "action_search", "available_actions"}:
            # Rewording a search that returns the same evidence is not another
            # route. Keep its source scope; judge actual changed results.
            for key in ("query", "q", "pattern", "glob", "case_sensitive"):
                normalized.pop(key, None)
            if name == "action_search":
                try:
                    payload = json.loads(result)
                    if isinstance(payload, dict):
                        payload.pop("query", None)
                        result = json.dumps(payload, sort_keys=True)
                except (ValueError, TypeError):
                    pass
        for key in ("path", "file_path", "directory", "cwd"):
            path = normalized.get(key)
            if isinstance(path, str) and path:
                expanded = os.path.expanduser(path)
                normalized[key] = os.path.normcase(os.path.abspath(os.path.join(self.cwd, expanded)))
        # Retain start_char/start_line/page/query: distinct ranges are useful even
        # when their text happens to be identical. No filesystem reread is needed.
        encoded = json.dumps([name, normalized, result], sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()

    def observe(
        self,
        name: str,
        args: dict[str, Any],
        result: str,
        *,
        status: str,
        invoked: bool,
        observation: bool,
        deduplicated: bool = False,
    ) -> None:
        self._batch_count += 1
        progressed = False
        if status == "succeeded" or (observation and invoked and status == "failed"):
            if observation:
                fingerprint = self._fingerprint(name, args, result)
                progressed = fingerprint not in self._seen
                self._seen[fingerprint] = None
                self._seen.move_to_end(fingerprint)
                if len(self._seen) > 512:
                    self._seen.popitem(last=False)
            elif invoked and not deduplicated:
                progressed = True
                self._seen.clear()
                self._recent.clear()
            if invoked and status == "succeeded":
                self._blockers.discard(name)
        if status in {"denied", "skipped", "failed", "timed_out", "unknown_outcome"}:
            # Only tool identifiers enter messages; never persist raw arguments,
            # results, paths or their fingerprints as telemetry or memory.
            if len(self._blockers) < 8 and name.isidentifier():
                self._blockers.add(name)
        self._recent.append(progressed)
        self._batch_progress = self._batch_progress or progressed

    def finish_batch(self) -> ProgressBoundary | None:
        if not self._batch_count:
            return None
        if self._batch_progress and self._warned:
            # Judge the alternative on its own evidence. Old repeated reads must
            # not force a stop after a genuinely productive strategy change.
            self._recent.clear()
            self._recent.append(True)
        self._stale_batches = 0 if self._batch_progress else self._stale_batches + 1
        self._batch_count = 0
        self._batch_progress = False
        mostly_repeated = len(self._recent) == self._recent.maxlen and sum(self._recent) <= 3
        stalled = self._stale_batches >= 3 or mostly_repeated
        if not stalled:
            self._warned = False
            self._warning_rounds = 0
            return None
        if self._warned:
            self._warning_rounds += 1
        stop = self._warned and self._warning_rounds >= 2
        if self._warned and not stop:
            return None
        self._warned = True
        reason = (
            "Tool progress stopped: repeated inspection or failed actions produced no new evidence."
            if stop else
            "Repeated inspection or failed actions are producing no new evidence."
        )
        if self._blockers:
            reason += " Unresolved unavailable or failed actions: " + ", ".join(sorted(self._blockers)) + "."
        return ProgressBoundary(stop=stop, reason=reason)
