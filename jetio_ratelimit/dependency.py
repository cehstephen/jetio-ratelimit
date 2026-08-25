"""Dependency mode: a Depends()-compatible callable usable directly in a
CrudRouter policy dict, or as a Depends() parameter on a hand-written route
-- for protecting endpoints WE register (order creation, etc.), as opposed
to routes AuthRouter owns internally (/login, /register -- those need
middleware mode, see middleware.py's docstring).
"""

from typing import Any, Optional

from jetio import Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from .keys import KeyContext, KeyFunc, by_ip
from .stores import RateLimitStore


def _resolve_client_ip(request) -> Optional[str]:
    """Request.client is public as of jetio 1.2.3 (cehstephen/jetio#4) --
    this package's minimum jetio version. No fallback needed; kept as its
    own function for the same reason it always was: a stable, single spot
    to read this from, testable without a real Request object."""
    client = getattr(request, "client", None)
    return client[0] if client else None


def make_dependency(
    store: RateLimitStore,
    name: str,
    max_attempts: int,
    window_seconds: float = 60,
    key_func: KeyFunc = by_ip,
    identity_dependency: Optional[Any] = None,
):
    """Build a Depends()-compatible rate-limit check.

    identity_dependency, if given (e.g. an AuthRouter's
    `auth.get_auth_dependency()`), is awaited first to resolve the caller;
    its result is exposed to key_func as ctx.user (see keys.by_user) and is
    also this dependency's own return value, so composing it into a
    CrudRouter policy value still gives CrudRouter a real user object to
    check truthiness on -- it doesn't need a *second*, separate auth
    dependency in the same policy slot.

    A blocked request raises starlette.exceptions.HTTPException(429,
    headers={"Retry-After": ...}). Jetio's exception handler propagates
    HTTPException.headers as of jetio 1.2.3 (cehstephen/jetio#3) -- this
    package's minimum version -- so a real Retry-After header reaches the
    client here, the same as middleware mode.
    """

    async def _dependency(request: Request, db: AsyncSession):
        user = None
        if identity_dependency is not None:
            user = await identity_dependency(request, db)

        ip = _resolve_client_ip(request)

        body = {}
        if request.method in ("POST", "PUT"):
            body = await request.json()

        # request.headers is a Starlette Headers object -- normalize to a
        # plain lowercase dict so by_header() behaves identically to
        # middleware mode regardless of the client's header casing.
        headers = {k.lower(): v for k, v in request.headers.items()}

        # `name` namespaces this limit's store keys -- see middleware.py's
        # RateLimitMiddleware for why that's required, not optional.
        ctx = KeyContext(ip=ip, body=body, user=user, headers=headers)
        key = f"{name}:{key_func(ctx)}"

        result = await store.hit(key, max_attempts, window_seconds)
        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests, retry after {result.retry_after_seconds}s",
                headers={"Retry-After": str(result.retry_after_seconds)},
            )

        return user if identity_dependency is not None else True

    return _dependency
