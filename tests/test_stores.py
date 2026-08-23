import pytest

from jetio_ratelimit.stores import InMemoryStore


@pytest.mark.asyncio
async def test_in_memory_store_allows_then_blocks():
    store = InMemoryStore()
    for _ in range(3):
        result = await store.hit("k", limit=3, window_seconds=60)
        assert result.allowed
    result = await store.hit("k", limit=3, window_seconds=60)
    assert not result.allowed


@pytest.mark.asyncio
async def test_in_memory_store_sweeps_after_threshold():
    import asyncio

    store = InMemoryStore(sweep_every=1)  # every hit triggers a sweep pass
    await store.hit("a", limit=5, window_seconds=0.01)  # too fresh to be swept yet
    assert "a" in store._counter._state

    await asyncio.sleep(0.05)  # now older than max_age_seconds = window_seconds * 2
    await store.hit("b", limit=5, window_seconds=0.01)  # its own sweep pass removes "a"
    assert "a" not in store._counter._state
    assert "b" in store._counter._state
