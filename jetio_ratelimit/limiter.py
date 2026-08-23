"""RateLimiter: the single entry point, mirroring how jetio_auth.AuthRouter
is the entry point for auth -- one object, two ways to apply it."""

from dataclasses import dataclass
from typing import Any, List, Optional

from .dependency import make_dependency
from .keys import KeyFunc, by_ip
from .middleware import RateLimitMiddleware
from .stores import InMemoryStore, RateLimitStore


@dataclass
class Limit:
    """One rate-limit rule, for stacking several onto one route via
    RateLimiter.protect_many(). Fields mirror .protect()'s per-call
    parameters (minus app/path/method, which protect_many() supplies once
    for the whole list)."""

    max_attempts: int
    window_seconds: float = 60
    key_func: KeyFunc = by_ip
    name: Optional[str] = None


class RateLimiter:
    """Holds one store (shared across every limit registered from it) and
    builds either middleware or a Depends()-compatible dependency on demand.

    ```python
    from jetio_ratelimit import RateLimiter, InMemoryStore, by_ip, by_field

    limiter = RateLimiter(store=InMemoryStore())

    # Middleware mode -- protects a route we don't own the handler for
    # (e.g. jetio-auth's AuthRouter registers /login internally). Two calls
    # stack IP + account keying -- either tripping blocks it. This is
    # exactly what protect_many() below collapses into one call; both forms
    # work, use whichever reads better at the call site.
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

    def protect_many(
        self,
        app,
        path: str,
        limits: List[Limit],
        method: str = "POST",
    ) -> None:
        """Registers several stacked limits on one path+method in one call
        -- e.g. IP + account keying on /login. Equivalent to calling
        .protect() once per Limit (each still gets its own auto-unique
        name unless a Limit sets one explicitly); this just removes the
        repetition of app/path/method across every stacked rule.

        The real payoff isn't the 2-lines-to-1 collapse on a single route,
        though -- it's that `limits` is a plain list you can define once
        and reuse across every route that should share one policy:

        ```python
        AUTH_ENDPOINT_POLICY = [
            Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
            Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
        ]
        for path in ["/login", "/register", "/reset-password"]:
            limiter.protect_many(app, path=path, limits=AUTH_ENDPOINT_POLICY)
        ```

        Without this, 30 endpoints needing the same 2-rule policy is 60
        near-identical .protect() calls -- tedious and easy to typo one
        limit out of sync with the rest. With it, it's one shared list and
        one loop; changing the policy means editing it in one place.
        """
        for limit in limits:
            self.protect(
                app,
                path=path,
                max_attempts=limit.max_attempts,
                window_seconds=limit.window_seconds,
                method=method,
                key_func=limit.key_func,
                name=limit.name,
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
