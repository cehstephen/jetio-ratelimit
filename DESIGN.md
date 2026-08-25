# jetio-ratelimit — Architecture & Design

A rate-limiting plugin for [Jetio](https://pypi.org/project/jetio/), architected
the same way as [jetio-auth](https://pypi.org/project/jetio-auth/): usable both
as global middleware and as a `Depends()`-injectable dependency, so it composes
with `CrudRouter(policy={...})` the same way `AuthPolicy.owner_or_admin()` does.

This document explains *why* the package is built the way it is. For usage,
see [docs/USAGE.md](docs/USAGE.md); for current limitations, see
[README.md](README.md#known-limitations).

## Design decisions

### 1. Algorithm: sliding window counter, not token bucket, not fixed window

Fixed window (counting hits in discrete, clock-aligned buckets) allows up to
2x the stated limit right at the window boundary — cheap to implement, but
has an exploitable edge.

Token bucket is the wrong *default* here: its entire point is letting clients
burst above the average rate before throttling kicks in. That's right for a
public data API; it's wrong for an endpoint like `/login`, where you don't
want to hand an attacker "burst credit," and it adds a second tunable
parameter (bucket size vs. refill rate) that's easy to misconfigure.

Sliding window counter (two rolling counters, weighted by how far into the
current window you are) is the industry-standard middle ground: nearly as
accurate as a full request log, O(1) state per key, no boundary exploit, and
"N attempts per rolling window" is easy to explain and hard to misconfigure.
This is the default. Token bucket may be added later as an opt-in algorithm
for non-auth, intentionally-bursty endpoints.

### 2. Keying: IP alone is not enough — key on IP *and* account identity

Real credential-stuffing tools don't hammer one IP; they spray one or two
attempts each across thousands of residential-proxy IPs specifically because
IP-based limiting is the default (and often only) defense in most systems.
An algorithm keyed only on IP looks fine in a demo and does little against a
real botnet.

The plugin supports stacking two limits on auth-adjacent endpoints: one keyed
by caller IP, one keyed by the account being targeted (e.g. the
`username`/`email` field in the request body) — whichever trips first wins.
Per-account keying is what actually stops a distributed attack; per-IP
keying mostly stops noisy single-source abuse and scraping.

### 3. Pluggable storage backend

An in-memory store is single-process only — every worker/replica gets its
own separate quota, which quietly weakens the limit by a factor of N behind
a load balancer. The plugin defines a small store interface so the default
(in-memory, zero dependencies) can be swapped for a shared one (Redis)
without changing call sites:

```python
class RateLimitStore(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> HitResult:
        """Record one hit for `key` and report whether it's over `limit`
        within the trailing `window_seconds`."""

@dataclass
class HitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int
```

`InMemoryStore` ships now. A `RedisStore` is planned before this is
production-ready for anyone running multiple workers — using
`redis.asyncio` (redis-py's native asyncio client; `aioredis` is deprecated
and merged into redis-py).

### 4. Dual API surface: middleware *and* dependency, from one implementation

Middleware alone covers "protect this whole route," matched by an exact path
string, but can't express "5 order-creates per user per minute" inside a
`CrudRouter` policy dict. Both modes are exposed from the same underlying
`RateLimiter`:

```python
from jetio_ratelimit import RateLimiter, InMemoryStore

limiter = RateLimiter(store=InMemoryStore())

# Whole-route protection
app.add_middleware(
    limiter.middleware(path="/login", max_attempts=5, window_seconds=60, key="ip+field:username"),
)

# Per-route composition -- usable directly in a CrudRouter policy dict,
# same pattern as AuthPolicy.owner_or_admin()
CrudRouter(
    model=Order,
    secure=True,
    policy={
        "POST": limiter.dependency(max_attempts=10, window_seconds=60, key="user"),
    },
).register_routes(app)
```

`CrudRouter.policy` holds exactly one callable per HTTP method (confirmed
from `jetio/framework.py`'s source — see "Jetio framework internals"
below), so combining a rate check with an auth check in one policy slot
needs one callable that does both, not a list. `.dependency()` handles this
via its `identity_dependency` parameter: given an auth dependency (e.g.
`auth.get_auth_dependency()`), it resolves auth first internally and
exposes the result to `by_user`, so one policy slot gets both checks.

## Threat model

| Decision | Threat it addresses | What it does *not* address |
|---|---|---|
| Sliding window over fixed window | Boundary-doubling exploit | Distributed attacks |
| Sliding window over token bucket | Burst-credit handed to attackers on sensitive endpoints | N/A -- token bucket is fine elsewhere |
| Account-keyed limit (stacked with IP) | Distributed credential stuffing (botnets, proxy pools) | Attacks against a single account from one IP (IP-keyed limit still needed for that) |
| Pluggable store (Redis option) | Quota silently multiplying by worker count behind a load balancer | N/A -- correctness issue, not a threat per se |

## Jetio framework internals (reference)

Documented here for anyone building another Jetio plugin, since none of
this is covered in Jetio's own docs. Verified against `jetio/framework.py`
source, accurate as of jetio 1.2.3.

- `app.add_middleware(cls, **kwargs)` does `self.app = cls(self.app, **kwargs)`
  — middleware wraps in reverse order of `add_middleware` calls.
- `jetio.BaseMiddleware.__call__(self, scope, receive, send)` is the ASGI
  entry point; `scope["type"]`, `scope["path"]`, `scope["method"]`,
  `scope["client"]` (a `(host, port)` tuple, may be `None`) are all
  available directly off the raw scope, before any `Request` object exists.
- A short-circuit response is built by constructing `JsonResponse(...)` and
  `await`-ing it directly as the ASGI callable (`await response(scope,
  receive, send)`) — the same pattern `CORSMiddleware` uses for its
  `OPTIONS` short-circuit.
- For `.dependency()` mode: Jetio's dependency-injection resolver passes
  named path params, `request`, and `db` into any `Depends()` callable by
  matching parameter *names* on the callable's own signature (see
  `jetio/framework.py`'s `handle_request`, the `sub_dep_kwargs` block). A
  dependency that needs the request body (e.g. to read `username` for
  account-keyed limiting) needs `request: Request` and to call
  `await request.json()` itself — the framework doesn't parse the body for
  dependencies the way it does for a route handler's Pydantic-typed
  parameter.
- `CrudRouter.policy` is `Dict[str, Callable]` — one callable per HTTP
  method, not a list. See "Design decision 4" above for how this package
  composes an auth check and a rate check into one policy slot as a result.

## Compatibility note: `from __future__ import annotations`

`jetio_auth/mixins.py` uses `from __future__ import annotations`, which
turns its class annotations into unevaluated strings. Jetio core's
`ModelMetaclass` doesn't re-resolve those, so any `JetioModel` subclass
built from a mixin defined with that import crashes with `SyntaxError:
Forward reference must be an expression`. This package's own classes aren't
`JetioModel` subclasses, so the bug doesn't apply directly here — but it's
worth flagging for any Jetio plugin whose classes might be inspected by
Jetio's metaclass: avoid `from __future__ import annotations` in those
modules, or resolve annotations before Jetio's metaclass collects them.

## Bugs identified and fixed

### Cross-limit key collisions (this package)

`InMemoryStore`/`SlidingWindowCounter` key state purely by the string a
`key_func` produces (e.g. `"ip:127.0.0.1"`). Two independently-registered
limits that happen to produce the same key string — which happens by
default any time `by_ip` is used more than once on the same `RateLimiter`
— would silently share one counter: a looser limit registered elsewhere
could starve or launder hits for a stricter one. This surfaced during
testing: a dependency-mode limit on order creation, defaulting to `by_ip`
instead of `by_user`, inherited an already-elevated count from an unrelated
IP-keyed middleware limit sharing the same store, blocking the first order
request.

**Fix**: every call to `RateLimiter.protect()`/`.dependency()` gets an
automatically-unique `name`, prefixed onto the store key
(`f"{name}:{key_func(ctx)}"`), so independently-registered limits can never
collide even when their raw keys coincide. An explicit, matching `name=`
passed to two calls is the only way to share state on purpose. See
`limiter.py`'s `_next_name` and the key construction in
`middleware.py`/`dependency.py`.

### Upstream Jetio bugs

Both found while integrating with Jetio, fixed and published upstream as of
jetio 1.2.3 (this package's minimum version):

- **`HTTPException.headers` was discarded by Jetio's exception handler.**
  `except StarletteHTTPException as e: response = JsonResponse({"detail":
  e.detail}, status_code=e.status_code)` never read `.headers`, so a raised
  `HTTPException(429, headers={"Retry-After": ...})` silently lost that
  header. Middleware mode was unaffected, since it builds the
  `JsonResponse` directly rather than raising through the framework's
  exception-handling path. Fixed in
  [cehstephen/jetio#3](https://github.com/cehstephen/jetio/pull/3) —
  `dependency.py` now raises with a real `Retry-After` header, verified in
  `tests_e2e/test_public_api_scenarios.py`.
- **`Request` exposed no public client/IP accessor**, only the private
  `_scope` attribute. Fixed in
  [cehstephen/jetio#4](https://github.com/cehstephen/jetio/pull/4), which
  adds a public `Request.client`. `dependency.py` uses it directly.

## Roadmap

- Progressive lockout on repeated violations — each violation extending the
  next lockout window (Auth0/Okta-style backoff), as a layer stacked on top
  of the window algorithm rather than a property of the algorithm itself.
  Raises attacker cost for cheap; not yet started.
- Dependency-mode stacking — `.dependency()` currently returns one callable
  for one policy slot. Stacking multiple checks there (the way
  `protect_many()` does for middleware) needs something that chains several
  checks inside one callable, since `CrudRouter.policy` holds one callable
  per method, not a list.
- `RedisStore` — the `RateLimitStore` protocol already supports swapping
  this in without touching `middleware.py`/`dependency.py`.
