"""Rate-limiting algorithms. Pure Python -- no Jetio dependency, so these
are unit-testable without a running app or event loop.
"""

import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class HitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class SlidingWindowCounter:
    """Sliding window counter: approximates a true sliding log using two
    fixed windows (current + previous), weighted by how far into the
    current window `now` falls.

    O(1) state per key -- one (window_index, prev_count, curr_count,
    last_seen) tuple -- unlike a full sliding log, which stores every
    request timestamp and costs O(limit) memory per key.

    Chosen over a fixed window (which allows up to 2x the stated limit
    right at the window boundary) and over token bucket (which is designed
    to let clients burst above the average rate -- the wrong default for
    an auth endpoint, where you don't want to hand an attacker burst
    credit). See ../DESIGN.md for the full reasoning.
    """

    def __init__(self) -> None:
        # key -> (window_index, prev_count, curr_count, last_seen)
        self._state: Dict[str, Tuple[int, int, int, float]] = {}

    def hit(
        self,
        key: str,
        limit: int,
        window_seconds: float,
        now: Optional[float] = None,
    ) -> HitResult:
        if limit <= 0:
            raise ValueError("limit must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")

        now = time.time() if now is None else now
        window_index = int(now // window_seconds)
        elapsed_in_window = now - (window_index * window_seconds)
        fraction_into_window = elapsed_in_window / window_seconds

        stored_index, prev_count, curr_count, _ = self._state.get(key, (window_index, 0, 0, now))

        if stored_index == window_index:
            pass  # same window: keep prev/curr as-is
        elif stored_index == window_index - 1:
            prev_count, curr_count = curr_count, 0  # roll forward one window
        else:
            prev_count, curr_count = 0, 0  # gap of 2+ windows: stale, reset

        # Every hit that reaches this point is recorded, whether or not it
        # ends up allowed -- an attacker hammering past the limit should
        # keep pushing retry_after out, not get free unmetered attempts.
        curr_count += 1
        self._state[key] = (window_index, prev_count, curr_count, now)

        weighted_count = prev_count * (1 - fraction_into_window) + curr_count
        allowed = weighted_count <= limit
        remaining = max(0, int(limit - weighted_count))

        if allowed:
            retry_after = 0
        else:
            # Conservative approximation: worst case is a fresh window, so
            # report the time left in the current one. A real client that
            # waits this long is guaranteed to be under the limit again.
            retry_after = int(window_seconds - elapsed_in_window) + 1

        return HitResult(allowed=allowed, remaining=remaining, retry_after_seconds=retry_after)

    def sweep(self, max_age_seconds: float, now: Optional[float] = None) -> int:
        """Remove state for keys not hit within `max_age_seconds`.

        Without this, a store that sees many one-off keys (e.g. a scanner
        rotating through thousands of source IPs) grows its dict forever.
        Call periodically -- InMemoryStore does this automatically every
        N hits. Returns the number of keys removed.
        """
        now = time.time() if now is None else now
        stale = [k for k, (_, _, _, last_seen) in self._state.items() if now - last_seen > max_age_seconds]
        for k in stale:
            del self._state[k]
        return len(stale)
