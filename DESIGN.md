# jetio-ratelimit — Design Plan

A rate-limiting plugin for [Jetio](https://pypi.org/project/jetio/), architected
the same way as [jetio-auth](https://pypi.org/project/jetio-auth/): usable both
as global middleware and as a `Depends()`-injectable dependency, so it composes
with `CrudRouter(policy={...})` the same way `AuthPolicy.owner_or_admin()` does.

**Status: v0.1 built and verified**, in this same folder (`jetio_ratelimit/`).
Originally written up as a plan after prototyping a single-file version
(`jetio.BaseMiddleware`-based, in-memory, IP-keyed, fixed-window) in
`ptester/playground/fixed-api/app.py` and finding its real limitations —
those limitations are what sections 1-5 below argue against. See README.md
for usage and current known limitations of the real implementation.

## Decisions made

### 1. Algorithm: sliding window counter, not token bucket, not fixed window

Fixed window (what the prototype uses) allows up to 2x the stated limit right
at the window boundary — cheap, but has an exploitable edge.

Token bucket is the wrong *default* here: its entire point is letting clients
burst above the average rate before throttling kicks in. That's right for a
public data API; it's wrong for an endpoint like `/login`, where you don't
want to hand an attacker "burst credit," and it adds a second tunable
parameter (bucket size vs refill rate) developers can misconfigure.

Sliding window counter (two rolling counters, weighted by how far into the
current window you are) is the industry-standard middle ground: nearly as
accurate as a full request log, O(1) state per key, no boundary exploit, and
"N attempts per rolling window" is easy to explain and hard to misconfigure.
**Default to this.** Token bucket can come later as an opt-in algorithm for
non-auth, intentionally-bursty endpoints.

### 2. Keying: IP alone is not enough — key on IP *and* account identity

Real credential-stuffing tools don't hammer one IP; they spray 1–2 attempts
each across thousands of residential-proxy IPs specifically because IP-based
limiting is the default (and often only) defense. Any algorithm keyed only
on IP looks fine in a demo and does little against a real botnet.

The plugin should support stacking two limits on auth-adjacent endpoints:
one keyed by caller IP, one keyed by the account being targeted (e.g. the
`username`/`email` field in the request body) — whichever trips first wins.
Per-account keying is what actually stops a distributed attack; per-IP
keying mostly stops noisy single-source abuse and scraping.

### 3. Progressive lockout on repeated violations

A flat "5/min forever" is trivially budgeted around by a patient attacker.
Each violation should extend the next lockout window (Auth0/Okta-style
backoff) — a separate layer stacked on top of the window algorithm, not a
property of the algorithm itself. Raises attacker cost for cheap.

### 4. Pluggable storage backend

The prototype's in-memory `dict`/`deque` is single-process only — every
worker/replica gets its own separate quota, which quietly weakens the limit
by a factor of N behind a load balancer. The plugin needs a small store
interface so the default (in-memory, zero dependencies) can be swapped for a
shared one (Redis) without changing call sites:

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

`InMemoryStore` ships first (parity with the current prototype, correct
single-process behavior). `RedisStore` ships before calling this
production-ready for anyone running multiple workers — use `redis.asyncio`
(redis-py's native asyncio client; `aioredis` is deprecated and merged into
redis-py, don't depend on it separately).

### 5. Dual API surface: middleware *and* dependency, from one implementation

The prototype is middleware-only, matched by an exact path string. That
covers "protect this whole route" but can't express "5 order-creates per
user per minute" inside a `CrudRouter` policy dict. Expose both from the
same underlying limiter object:

```python
from jetio_ratelimit import RateLimiter, InMemoryStore

limiter = RateLimiter(store=InMemoryStore())

# Whole-route protection (same shape as the current prototype)
app.add_middleware(
    limiter.middleware(path="/login", max_attempts=5, window_seconds=60, key="ip+field:username"),
)

# Per-route composition -- usable directly in a CrudRouter policy dict,
# same pattern as AuthPolicy.owner_or_admin()
CrudRouter(
    model=Order,
    secure=True,
    policy={
        "POST": [auth.get_auth_dependency(), limiter.dependency(max_attempts=10, window_seconds=60, key="user")],
    },
).register_routes(app)
```

(Whether `policy` values can already be a list of chained dependencies, or
only a single callable, needs checking against the current `CrudRouter`
implementation before finalizing this shape — see Open Questions.)

## Package layout

Mirrors jetio-auth's module split:

```
jetio_ratelimit/
  __init__.py        # public exports: RateLimiter, InMemoryStore, RedisStore
  limiter.py          # RateLimiter -- the AuthRouter-equivalent entry point
  algorithms.py       # SlidingWindowCounter (default), FixedWindow, TokenBucket (later)
  stores.py           # RateLimitStore protocol + InMemoryStore + RedisStore
  middleware.py        # ASGI middleware wrapper (jetio.BaseMiddleware subclass)
  keys.py             # key functions: by_ip, by_field("username"), combine(...)
```

## Relevant verified Jetio internals

(Confirmed by reading `jetio/framework.py` source while building the
prototype -- accurate as of jetio 1.2.2, re-verify if it's moved on.)

- `app.add_middleware(cls, **kwargs)` does `self.app = cls(self.app, **kwargs)`
  — middleware wraps in reverse order of `add_middleware` calls.
- `jetio.BaseMiddleware.__call__(self, scope, receive, send)` is the ASGI
  entry point; `scope["type"]`, `scope["path"]`, `scope["method"]`,
  `scope["client"]` (a `(host, port)` tuple, may be `None`) are all
  available directly off the raw scope, before any `Request` object exists.
- A short-circuit response is built by constructing `JsonResponse(...)` and
  `await`-ing it directly as the ASGI callable (`await response(scope,
  receive, send)`) — same pattern `CORSMiddleware` uses for its `OPTIONS`
  short-circuit.
- For the `.dependency()` mode: Jetio's dependency-injection resolver passes
  named path params, `request`, and `db` into any `Depends()` callable by
  matching parameter *names* on the callable's own signature (see
  `jetio/framework.py`'s `handle_request`, the `sub_dep_kwargs` block). A
  dependency wanting the request body (e.g. to read `username` for
  account-keyed limiting) needs `request: Request` and to call
  `await request.json()` itself — the framework doesn't parse the body for
  dependencies the way it does for a route handler's Pydantic-typed param.

## Known pitfall to avoid (lesson from jetio-auth)

`jetio_auth/mixins.py` uses `from __future__ import annotations`, which
turns its class annotations into unevaluated strings. Jetio core's
`ModelMetaclass` doesn't re-resolve those, so any `JetioModel` subclass
built from a mixin defined with that import crashes with `SyntaxError:
Forward reference must be an expression`. `jetio-ratelimit`'s own classes
aren't `JetioModel` subclasses, so this exact bug doesn't apply directly —
but don't add `from __future__ import annotations` to any module whose
classes might later get inspected by Jetio's metaclass, and consider
flagging the upstream fix (drop the import, or resolve annotations before
collecting them) since it'll bite the next plugin too.

## Threat model summary (why each decision exists)

| Decision | Threat it addresses | What it does *not* address |
|---|---|---|
| Sliding window over fixed window | Boundary-doubling exploit | Distributed attacks |
| Sliding window over token bucket | Burst-credit handed to attackers on sensitive endpoints | N/A -- token bucket is fine elsewhere |
| Account-keyed limit (stacked with IP) | Distributed credential stuffing (botnets, proxy pools) | Attacks against a single account from one IP (IP-keyed limit still needed for that) |
| Progressive lockout | Patient, budget-aware attackers | First-attempt burst (window algorithm's job) |
| Pluggable store (Redis option) | Quota silently multiplying by worker count behind a load balancer | N/A -- correctness issue, not a threat per se |

## Open questions — resolved during the build

- ~~Can `CrudRouter`'s `policy` values accept a list/chain of dependencies?~~
  **No** — confirmed from source: `self.policy: Dict[str, Callable] = {}`,
  one callable per method. Didn't need a generic `combine()` helper though:
  `RateLimiter.dependency(identity_dependency=...)` takes an auth dependency
  directly and calls it internally, resolving both auth and the rate check
  from one policy slot — simpler than composing two separate callables.
- Default numbers shipped in `examples/demo_app.py`: 5/min per IP + 3/min
  per account on `/login`, 3/min per user on order creation — illustrative,
  not a recommendation; still needs validation against real traffic.
- Package name `jetio-ratelimit`, BSD-3-Clause — set in `pyproject.toml`.

## Real bug found while building it (not in Jetio this time — in this design)

`InMemoryStore`/`SlidingWindowCounter` key state purely by the string
`key_func` produces (e.g. `"ip:127.0.0.1"`). Two independently-registered
limits that happen to produce the same key string — which happens by
default any time `by_ip` is used more than once, e.g. stacking `/login`'s
IP-keyed limit with a *different* endpoint's default-`by_ip` limit sharing
one `RateLimiter`/store — would silently share one counter, so a looser
limit registered elsewhere could either starve or launder hits for a
stricter one. Caught live: a demo dependency-mode limit on order creation
defaulted to `by_ip` (forgot to pass `key_func=by_user`) and inherited an
already-elevated count from the `/login` IP-keyed middleware tests, blocking
the very first order request. **Fix**: every call to `RateLimiter.protect()`
/`.dependency()` now gets an automatically-unique `name`, prefixed onto the
store key (`f"{name}:{key_func(ctx)}"`), so independently-registered limits
can never collide even when their raw keys coincide — only an explicit,
matching `name=` passed to two calls shares state on purpose. See
`limiter.py`'s `_next_name` and both `middleware.py`/`dependency.py`'s key
construction.

## Verified real bugs found in Jetio / jetio-auth while building this

- **`HTTPException.headers` is discarded by Jetio core.** Confirmed from
  `jetio/framework.py`: `except StarletteHTTPException as e: response =
  JsonResponse({"detail": e.detail}, status_code=e.status_code)` — no
  `.headers`. A raised `HTTPException(429, headers={"Retry-After": ...})`
  silently loses that header. `dependency.py` works around it by embedding
  the retry time in the message text; middleware mode is unaffected since it
  builds the `JsonResponse` directly rather than raising through the
  framework's exception-handling path. Worth a real fix upstream.
- **`Request` exposes no public client/IP accessor.** Only `self._scope`
  (private). `dependency.py`'s `by_ip` support reads
  `request._scope["client"]` — works, but is reaching into private API by
  necessity. Worth Jetio core exposing `request.client` publicly; other
  consumers (logging, audit trails) will hit the same wall.

## Build order

1. ~~`SlidingWindowCounter` + `InMemoryStore`~~ — done (`algorithms.py`,
   `stores.py`), unit-tested (`tests/`).
2. ~~`.dependency()` mode, wired into a real `CrudRouter` policy~~ — done,
   verified live in `examples/demo_app.py` (per-user order-creation limit,
   composed with `auth.get_auth_dependency()`).
3. ~~Account-keyed limiting + stacked IP+account limits on `/login`~~ —
   done, verified live: account-keyed limit trips independently of the
   IP-keyed one, and a different account from the same IP is unaffected.
3b. ~~`Limit` + `RateLimiter.protect_many()`~~ — done. Raised by the user:
   stacking N limits on one route was N `.protect()` calls, and repeating
   the same stacked policy across many routes multiplied that further (30
   endpoints x 2 rules = 60 near-identical lines). `protect_many(app, path,
   limits: List[Limit])` collapses the per-route case to one call; the
   bigger win is `limits` being a reusable list, so one shared policy can be
   applied to many routes in a loop instead of duplicated. `.protect()`
   itself is untouched — fully backward compatible, `protect_many()` is a
   thin loop over it. No dependency-mode equivalent yet (see below).
3c. ~~`by_header()` key function + `KeyContext.headers`~~ — done. Surfaced
   while writing docs/USAGE.md: there was no way to key by a request header
   (e.g. `X-API-Key` for machine-to-machine endpoints), only IP, body
   fields, or resolved user. Added `headers: Dict[str, str]` (lowercase
   keys) to `KeyContext`, populated in both modes (raw ASGI header list in
   middleware.py, `request.headers` in dependency.py), plus `by_header(name)`
   mirroring `by_field(name)`. Verified live, including on a `GET` route
   (proving `.protect()` isn't POST-only, just POST-by-default).
4. Progressive lockout layer — not started.
4b. Dependency-mode stacking — not started. `.dependency()` returns one
   callable for one policy slot; stacking multiple checks there (the way
   `protect_many()` does for middleware) would need something that chains
   several checks inside one callable, since `CrudRouter.policy` holds one
   callable per method, not a list (confirmed from source, see above).
5. `RedisStore` — not started; `RateLimitStore` protocol already supports
   swapping it in without touching `middleware.py`/`dependency.py`.
6. Publish `v0.1.0` to PyPI; retrofit `playground/fixed-api`
   (`ptester/playground/fixed-api/app.py`) to depend on this package instead
   of its inline `RateLimitMiddleware` prototype.
