from jetio_ratelimit import InMemoryStore, Limit, RateLimiter, by_field, by_ip


class FakeApp:
    """Minimal stand-in for a Jetio app -- just records add_middleware calls
    so protect_many() can be tested without a real ASGI app/server."""

    def __init__(self):
        self.registered = []

    def add_middleware(self, cls, **kwargs):
        self.registered.append((cls, kwargs))


def test_protect_many_registers_one_middleware_per_limit():
    app = FakeApp()
    limiter = RateLimiter(store=InMemoryStore())

    limiter.protect_many(
        app,
        path="/login",
        limits=[
            Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
            Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
        ],
    )

    assert len(app.registered) == 2
    names = [kwargs["name"] for _, kwargs in app.registered]
    assert len(set(names)) == 2, "each stacked limit must get a distinct store-key namespace"
    max_attempts = [kwargs["max_attempts"] for _, kwargs in app.registered]
    assert max_attempts == [5, 3]


def test_protect_many_is_equivalent_to_calling_protect_twice():
    app_a = FakeApp()
    app_b = FakeApp()
    limiter_a = RateLimiter(store=InMemoryStore())
    limiter_b = RateLimiter(store=InMemoryStore())

    limiter_a.protect(app_a, path="/login", max_attempts=5, window_seconds=60, key_func=by_ip)
    limiter_a.protect(app_a, path="/login", max_attempts=3, window_seconds=60, key_func=by_field("username"))

    limiter_b.protect_many(
        app_b,
        path="/login",
        limits=[
            Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
            Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
        ],
    )

    a_shapes = [(kw["max_attempts"], kw["window_seconds"], kw["path"]) for _, kw in app_a.registered]
    b_shapes = [(kw["max_attempts"], kw["window_seconds"], kw["path"]) for _, kw in app_b.registered]
    assert a_shapes == b_shapes


def test_reused_limits_list_is_independent_per_path():
    app = FakeApp()
    limiter = RateLimiter(store=InMemoryStore())
    policy = [Limit(max_attempts=5, window_seconds=60, key_func=by_ip)]

    limiter.protect_many(app, path="/login", limits=policy)
    limiter.protect_many(app, path="/register", limits=policy)

    names = [kwargs["name"] for _, kwargs in app.registered]
    assert len(set(names)) == 2, "same Limit object reused on two paths must still get distinct namespaces"
