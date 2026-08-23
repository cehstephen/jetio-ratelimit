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

    Note: a blocked request raises starlette.exceptions.HTTPException(429).
    Jetio core's exception handler currently discards HTTPException.headers
    (verified against jetio 1.2.2 -- it only reads .detail/.status_code), so
    the Retry-After value is embedded in the message text here instead of a
    header. Middleware mode does not have this limitation, since it builds
    the 429 JsonResponse directly rather than raising through the framework's
    exception-handling path -- worth a real fix upstream.
    """

    async def _dependency(request: Request, db: AsyncSession):
        user = None
        if identity_dependency is not None:
            user = await identity_dependency(request, db)

        scope = getattr(request, "_scope", {})
        client = scope.get("client")
        ip = client[0] if client else None

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
            )

        return user if identity_dependency is not None else True

    return _dependency
