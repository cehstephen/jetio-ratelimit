"""Key functions: decide what identity a rate limit is scoped to.

Deliberately framework-agnostic (pure functions over KeyContext) so they're
testable without an ASGI scope or a running app, and so the same key
function works from both middleware mode and dependency mode.

by_ip and by_field are meant to be applied as two SEPARATE, stacked limits
(register the limiter twice) on a sensitive endpoint like /login -- not
merged into one compound key. Merging (ip, username) into a single key
would let an attacker rotating IPs while keeping the target username fixed
get a fresh key on every attempt, defeating the point. See ../DESIGN.md.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class KeyContext:
    ip: Optional[str]
    body: Dict[str, Any] = field(default_factory=dict)
    user: Optional[Any] = None


KeyFunc = Callable[[KeyContext], str]


def by_ip(ctx: KeyContext) -> str:
    return f"ip:{ctx.ip or 'unknown'}"


def by_field(field_name: str) -> KeyFunc:
    """Key by a field in the request body, e.g. by_field('username') to
    scope a limit to the account being targeted rather than the caller."""

    def _key(ctx: KeyContext) -> str:
        value = ctx.body.get(field_name)
        return f"{field_name}:{value}" if value else f"{field_name}:missing"

    return _key


def by_user(ctx: KeyContext) -> str:
    """Key by the authenticated user's id. Only meaningful when the
    limiter was built with an identity_dependency (see RateLimiter.dependency);
    without one, ctx.user is always None and every caller shares one key."""
    user_id = getattr(ctx.user, "id", None)
    return f"user:{user_id}" if user_id is not None else "user:anonymous"
