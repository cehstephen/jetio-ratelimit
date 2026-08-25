"""Pluggable storage backends for rate-limit state.

InMemoryStore is correct for a single worker process only: behind multiple
workers or replicas, each process holds its own counters, so the effective
limit silently becomes `limit * worker_count`. A RedisStore (sharing state
across processes) is planned -- see ../DESIGN.md's Roadmap -- but not
yet implemented; anyone running more than one worker should not treat
InMemoryStore as sufficient.
"""

from typing import Protocol, runtime_checkable

from .algorithms import HitResult, SlidingWindowCounter


@runtime_checkable
class RateLimitStore(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: float) -> HitResult:
        """Record one hit for `key` and report whether it's over `limit`
        within the trailing `window_seconds`."""
        ...


class InMemoryStore:
    """Process-local sliding window counters. Zero dependencies, correct
    for a single worker. See module docstring for the multi-worker caveat.
    """

    def __init__(self, sweep_every: int = 1000):
        self._counter = SlidingWindowCounter()
        self._sweep_every = sweep_every
        self._hits_since_sweep = 0

    async def hit(self, key: str, limit: int, window_seconds: float) -> HitResult:
        result = self._counter.hit(key, limit, window_seconds)

        self._hits_since_sweep += 1
        if self._hits_since_sweep >= self._sweep_every:
            self._counter.sweep(max_age_seconds=window_seconds * 2)
            self._hits_since_sweep = 0

        return result
