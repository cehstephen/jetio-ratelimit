from jetio_ratelimit.algorithms import SlidingWindowCounter


def test_allows_up_to_limit():
    c = SlidingWindowCounter()
    for i in range(5):
        result = c.hit("k", limit=5, window_seconds=60, now=1000.0)
        assert result.allowed, f"hit {i} should be allowed"


def test_blocks_over_limit_within_same_window():
    c = SlidingWindowCounter()
    for _ in range(5):
        c.hit("k", limit=5, window_seconds=60, now=1000.0)
    result = c.hit("k", limit=5, window_seconds=60, now=1000.0)
    assert not result.allowed
    assert result.retry_after_seconds > 0


def test_no_boundary_doubling_like_fixed_window():
    # Fixed window would allow 5 hits right before a window boundary and
    # another 5 right after -- 10 in a moment. Sliding window must not.
    c = SlidingWindowCounter()
    window = 60
    # Fill the limit just before the window boundary (window index 0 -> [0, 60))
    for _ in range(5):
        c.hit("k", limit=5, window_seconds=window, now=59.9)
    # Immediately after the boundary (window index 1 -> [60, 120)), the
    # previous window's hits should still weigh heavily since we're at the
    # very start of the new window.
    result = c.hit("k", limit=5, window_seconds=window, now=60.1)
    assert not result.allowed, "sliding window let a fixed-window-style burst through"


def test_recovers_after_window_fully_elapses():
    c = SlidingWindowCounter()
    window = 60
    for _ in range(5):
        c.hit("k", limit=5, window_seconds=window, now=0.0)
    # Two full windows later, prior activity should have fully decayed.
    result = c.hit("k", limit=5, window_seconds=window, now=130.0)
    assert result.allowed


def test_different_keys_are_independent():
    c = SlidingWindowCounter()
    for _ in range(5):
        c.hit("attacker", limit=5, window_seconds=60, now=1000.0)
    result = c.hit("victim", limit=5, window_seconds=60, now=1000.0)
    assert result.allowed


def test_sweep_removes_stale_keys_only():
    c = SlidingWindowCounter()
    c.hit("old", limit=5, window_seconds=60, now=0.0)
    c.hit("fresh", limit=5, window_seconds=60, now=1000.0)
    removed = c.sweep(max_age_seconds=120, now=1000.0)
    assert removed == 1
    assert "old" not in c._state
    assert "fresh" in c._state


def test_rejects_invalid_params():
    c = SlidingWindowCounter()
    try:
        c.hit("k", limit=0, window_seconds=60)
        assert False, "should have raised"
    except ValueError:
        pass
    try:
        c.hit("k", limit=5, window_seconds=0)
        assert False, "should have raised"
    except ValueError:
        pass
