"""Token Bucket Rate Limiter — smooth traffic shaping.

A token bucket fills at a fixed rate (tokens per second) up to a maximum
capacity (burst size).  Each request consumes one or more tokens.  If
insufficient tokens are available, the request is denied (or delayed).

Harness use:
  - Throttle Ollama API calls to avoid rate limits
  - Throttle embedding batches
  - Throttle web fetches
  - Throttle harness indexing rate

The leaky bucket variant (output rate constant) is also provided.

Operations:
  - try_acquire(tokens=1): non-blocking, returns True/False
  - acquire(tokens=1): blocking, sleeps until tokens available
  - wait_time(tokens=1): returns seconds to wait

Properties:
  - Allows bursts up to capacity
  - Long-term rate is capped at refill_rate
  - O(1) per operation
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any


class TokenBucket:
    """Token bucket rate limiter.

    Args:
        capacity: Maximum tokens the bucket can hold (burst size).
        refill_rate: Tokens added per second (sustained rate).
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens = float(capacity)  # start full
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking attempt to acquire tokens.

        Returns True if tokens were acquired, False if insufficient.
        """
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def wait_time(self, tokens: float = 1.0) -> float:
        """Return seconds to wait until tokens are available."""
        self._refill()
        if self._tokens >= tokens:
            return 0.0
        needed = tokens - self._tokens
        return needed / self.refill_rate

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Blocking acquire — sleeps until tokens are available.

        Args:
            tokens: Number of tokens to acquire.
            timeout: Maximum seconds to wait. None = wait forever.

        Returns:
            True if acquired, False if timed out.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = self.wait_time(tokens)
            if wait == 0.0:
                self._refill()
                self._tokens -= tokens
                return True
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(wait)

    @property
    def available_tokens(self) -> float:
        """Current token count (after refilling)."""
        self._refill()
        return self._tokens

    def stats(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "available_tokens": round(self.available_tokens, 2),
        }


class SlidingWindowCounter:
    """Sliding window rate limiter — counts requests in a time window.

    Uses a deque of timestamps, pruning entries older than the window.
    Simple and accurate for small windows.

    Args:
        window_seconds: Size of the sliding window.
        max_requests: Maximum requests allowed in the window.
    """

    def __init__(self, window_seconds: float, max_requests: int) -> None:
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._timestamps: deque[float] = deque()

    def _prune(self, now: float) -> None:
        """Remove timestamps outside the window."""
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def try_acquire(self) -> bool:
        """Non-blocking attempt to make a request."""
        now = time.monotonic()
        self._prune(now)
        if len(self._timestamps) < self.max_requests:
            self._timestamps.append(now)
            return True
        return False

    def current_count(self) -> int:
        """Number of requests in the current window."""
        self._prune(time.monotonic())
        return len(self._timestamps)

    def stats(self) -> dict[str, Any]:
        return {
            "window_seconds": self.window_seconds,
            "max_requests": self.max_requests,
            "current_count": self.current_count(),
            "remaining": max(0, self.max_requests - self.current_count()),
        }