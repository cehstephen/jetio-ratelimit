"""RateLimiter: the single entry point, mirroring how jetio_auth.AuthRouter
is the entry point for auth -- one object, two ways to apply it."""

from typing import Any, Optional

from .dependency import make_dependency
from .keys import KeyFunc, by_ip
from .middleware import RateLimitMiddleware
from .stores import InMemoryStore, RateLimitStore


class RateLimiter:
    """Holds one store (shared across every limit registered from it) and
    builds either middleware or a Depends()-compatible dependency on demand.

    ```python
    from jetio_ratelimit import RateLimiter, InMemoryStore, by_ip, by_field

    limiter = RateLimiter(store=InMemoryStore())

    # Middleware mode -- protects a route we don't own the handler for
    # (e.g. jetio-auth's AuthRouter registers /login internally). Call
    # .protect() twice for IP + account keying -- either tripping blocks it.
    limiter.protect(app, path="/login", max_attempts=5, window_seconds=60, key_func=by_ip)
    limiter.protect(app, path="/login", max_attempts=10, window_seconds=3600, key_func=by_field("username"))

    # Dependency mode -- composes into a CrudRouter policy dict
    CrudRouter(
        model=Order,
        secure=True,
        policy={"POST": limiter.dependency(max_attempts=10, window_seconds=60, identity_dependency=auth.get_auth_dependency())},
    ).register_routes(app)
    ```
    """

    def __init__(self, store: Optional[RateLimitStore] = None):
        self.store = store or InMemoryStore()
        self._rule_count = 0

    def _next_name(self, hint: str) -> str:
        # Every call gets a distinct name by default, even when two rules
        # target the same route (the /login IP+account stacking case) --
        # a shared store keyed only by key_func's output would otherwise let
        # unrelated rules silently borrow each other's hit counts. `hint` is
        # just for readability in debugging/logs, the counter is what
        # actually guarantees uniqueness.
        self._rule_count += 1
        return f"{hint}#{self._rule_count}"

    def protect(
        self,
        app,
        path: str,
        max_attempts: int,
        window_seconds: float = 60,
        method: str = "POST",
        key_func: KeyFunc = by_ip,
        name: Optional[str] = None,
    ) -> None:
        """Registers a rate limit on `path`+`method` via `app.add_middleware`.
        Call more than once (different key_func/limits) to stack independent
        limits on the same route -- e.g. IP + account keying on /login. Each
        call is isolated automatically; pass `name` explicitly only if you
        deliberately want two calls to share one counter."""
        app.add_middleware(
            RateLimitMiddleware,
            store=self.store,
            name=name or self._next_name(f"{method.upper()}:{path}"),
            path=path,
            max_attempts=max_attempts,
            window_seconds=window_seconds,
            method=method,
            key_func=key_func,
        )

    def dependency(
        self,
        max_attempts: int,
        window_seconds: float = 60,
        key_func: KeyFunc = by_ip,
        identity_dependency: Optional[Any] = None,
        name: Optional[str] = None,
    ):
        return make_dependency(
            store=self.store,
            name=name or self._next_name("dependency"),
            max_attempts=max_attempts,
            window_seconds=window_seconds,
            key_func=key_func,
            identity_dependency=identity_dependency,
        )
