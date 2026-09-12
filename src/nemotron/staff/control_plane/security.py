from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class SlidingWindowRateLimiter:
    """Process-local limiter suitable for the current single-replica control plane."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(
        self,
        bucket: str,
        client_key: str,
        *,
        limit: int,
        window_seconds: int = 60,
    ) -> RateLimitDecision:
        if limit < 1:
            return RateLimitDecision(False, window_seconds)
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (bucket, client_key)
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry = max(1, int(window_seconds - (now - events[0])) + 1)
                return RateLimitDecision(False, retry)
            events.append(now)
            return RateLimitDecision(True, 0)
