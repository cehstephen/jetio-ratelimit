# jetio-ratelimit — Usage Guide

Every example below has been run against a real `jetio`/`jetio-auth` app and
checked with curl, the same way the rest of this package was built — none
of this is speculative. For the *why* behind the design (sliding window vs
token bucket, IP-vs-account keying, the bugs found in Jetio along the way),
see [DESIGN.md](../DESIGN.md). This doc is the *how*.

## Table of contents

- [Core concepts](#core-concepts)
- [Middleware mode](#middleware-mode) — protecting a route you don't own the handler for
- [Dependency mode](#dependency-mode) — protecting a route you do
- [Key functions](#key-functions) — what identity a limit is scoped to
- [Handling a 429](#handling-a-429)
- [Choosing limits and windows](#choosing-limits-and-windows)
- [Testing your integration](#testing-your-integration)
- [Troubleshooting](#troubleshooting)

## Core concepts

```python
from jetio_ratelimit import RateLimiter, InMemoryStore

limiter = RateLimiter(store=InMemoryStore())
```

One `RateLimiter` holds one `store` (where hit counts live) and is the
thing you call `.protect()`, `.protect_many()`, or `.dependency()` on. You
can create more than one `RateLimiter` (e.g. with different stores) if you
want fully separate quota pools, but for one app, one `RateLimiter` shared
across every route is the normal case — every example below assumes that.

`InMemoryStore` is the only store that exists right now. It's correct for
one worker process; behind more than one, see
[Known limitations](../README.md#known-limitations) in the README.

## Middleware mode

Use this for routes you don't register yourself — the main case is
jetio-auth's `AuthRouter`, which owns `/login`, `/register`, and friends
internally. There's no `Depends()` hook to attach to on a route someone
else's code registers, so middleware (which wraps the whole ASGI app,
regardless of which code registered which route) is the only way in.

### One limit on one route

```python
from jetio_ratelimit import RateLimiter, InMemoryStore, by_ip

limiter = RateLimiter(store=InMemoryStore())
limiter.protect(app, path="/login", max_attempts=5, window_seconds=60, key_func=by_ip)
```

`method` defaults to `"POST"`. Pass `method="GET"` (or any other verb) for
routes that aren't POST:

```python
limiter.protect(app, path="/search", method="GET", max_attempts=30, window_seconds=60)
```

### Stacking multiple limits on one route

Register the same route more than once with different `key_func`/limits —
each call is isolated automatically, and a request is blocked if *any*
stacked limit trips:

```python
from jetio_ratelimit import by_field

limiter.protect(app, path="/login", max_attempts=5, window_seconds=60, key_func=by_ip)
limiter.protect(app, path="/login", max_attempts=3, window_seconds=60, key_func=by_field("username"))
```

This is the headline pattern: an IP-keyed limit alone barely slows down
real credential stuffing (attackers spread attempts across many IPs
specifically to dodge it); stacking an account-keyed limit alongside it
catches that case, since the account being targeted doesn't change even
when the attacker's IP does.

`protect_many()` collapses the two calls above into one, using a list of
`Limit`:

```python
from jetio_ratelimit import Limit, by_ip, by_field

limiter.protect_many(app, path="/login", limits=[
    Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
    Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
])
```

### Reusing one policy across many routes

`limits` is a plain list — define it once, reuse it everywhere the same
policy applies:

```python
AUTH_POLICY = [
    Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
    Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
]

for path in ["/login", "/register", "/reset-password"]:
    limiter.protect_many(app, path=path, limits=AUTH_POLICY)
```

This is the answer to "30 endpoints needing the same policy" — one shared
list and a loop, instead of 60 near-identical `.protect()` calls that can
silently drift out of sync with each other over time.

### Explicitly sharing state between two calls (rare)

Every `.protect()`/`.protect_many()` call gets its own automatically-unique
counter by default — two calls never accidentally share state, even if
their `key_func` happens to produce the same string. If you deliberately
*want* two registrations to share one counter (e.g. two slightly different
paths that should count against one combined quota), pass the same `name=`
to both:

```python
limiter.protect(app, path="/api/v1/upload", max_attempts=10, window_seconds=60, name="uploads")
limiter.protect(app, path="/api/v2/upload", max_attempts=10, window_seconds=60, name="uploads")
```

Leave `name` unset unless you specifically want this — it's an opt-in, not
something you need to think about for normal use.

## Dependency mode

Use this for routes you register yourself: a `CrudRouter` policy, or a
hand-written `@app.route(...)` using `Depends()` directly.

### In a CrudRouter policy, IP-keyed

```python
from jetio_ratelimit import by_ip

CrudRouter(
    model=Order,
    secure=True,
    policy={
        "POST": limiter.dependency(max_attempts=10, window_seconds=60, key_func=by_ip),
    },
).register_routes(app)
```

### Composed with jetio-auth, keyed by the authenticated user

Pass `identity_dependency` (any auth dependency, typically
`auth.get_auth_dependency()` from jetio-auth's `AuthRouter`) and use
`by_user` as the key function. The identity check runs first; its result
is both what gets keyed on *and* what's returned to `CrudRouter` (so the
policy slot gets real authentication and rate limiting from one callable —
no need for a second dependency in the same slot):

```python
from jetio_ratelimit import by_user

CrudRouter(
    model=Order,
    secure=True,
    policy={
        "POST": limiter.dependency(
            max_attempts=10, window_seconds=60,
            key_func=by_user,
            identity_dependency=auth.get_auth_dependency(),
        ),
    },
).register_routes(app)
```

Without `identity_dependency`, `ctx.user` is always `None` and `by_user`
keys everyone under `"user:anonymous"` — one shared quota for every caller.
Always pass `identity_dependency` when using `by_user`.

### On a hand-written route, without CrudRouter

`.dependency()` returns a plain `Depends()`-compatible callable — it works
on any route, not just inside a `CrudRouter` policy:

```python
from jetio import Depends

rate_limited = limiter.dependency(max_attempts=2, window_seconds=60, key_func=by_ip)

@app.route("/ping", methods=["POST"])
async def ping(request: Request, ok=Depends(rate_limited)):
    return {"pong": True}
```

## Key functions

Every key function is a plain `Callable[[KeyContext], str]`. `KeyContext`
carries whatever the request offers:

```python
@dataclass
class KeyContext:
    ip: Optional[str]
    body: Dict[str, Any]       # parsed JSON body (empty dict for GET/DELETE)
    user: Optional[Any]        # set only when dependency mode has identity_dependency
    headers: Dict[str, str]    # lowercase header names -> value
```

| Function | Keys by | Typical use |
|---|---|---|
| `by_ip` | caller's IP | Default; catches noisy single-source abuse |
| `by_field(name)` | a field in the JSON body | Account-keyed limits on `/login` (`by_field("username")`) |
| `by_header(name)` | a request header | API-key-authenticated endpoints (`by_header("x-api-key")`) |
| `by_user` | `ctx.user.id` | Per-authenticated-user limits (needs `identity_dependency` in dependency mode) |

### Writing a custom key function

Any callable matching the signature works — no registration needed:

```python
def by_ip_and_endpoint_tier(ctx: KeyContext) -> str:
    tier = ctx.headers.get("x-plan-tier", "free")
    return f"{tier}:{ctx.ip or 'unknown'}"

limiter.protect(app, path="/api/generate", max_attempts=100 if tier == "paid" else 10, ...)
```

(That specific example needs the tier known before choosing `max_attempts`,
which happens outside the key function — a common pattern is picking the
*limit* per caller class in your route-registration code and using a
simpler key function like `by_header("x-api-key")` for the actual key.)

## Handling a 429

The two modes respond differently — a real, current limitation, not a
design choice:

**Middleware mode** returns a proper HTTP response with a `Retry-After`
header:

```
HTTP/1.1 429 Too Many Requests
retry-after: 42
content-type: application/json

{"error": "Too many attempts, try again later."}
```

**Dependency mode** raises `starlette.exceptions.HTTPException(429)`.
Jetio core's exception handler currently discards `HTTPException.headers`
(confirmed against jetio 1.2.2), so there's no `Retry-After` header — the
retry time is embedded in the message text instead:

```
HTTP/1.1 429 Too Many Requests
content-type: application/json

{"detail": "Too many requests, retry after 38s"}
```

If your client needs a machine-readable retry time from a dependency-mode
route, parse it out of `detail` for now (`retry after (\d+)s`), or prefer
middleware mode for routes where that matters until Jetio core exposes
`HTTPException.headers` (see DESIGN.md).

## Choosing limits and windows

There's no universal right number — these are starting points, not
recommendations, and DESIGN.md has the full threat-model reasoning:

- **Auth endpoints** (`/login`, password reset): tight and stacked — e.g.
  5/min per IP *and* 3/min per account. Low false-positive cost (a real
  user retrying 3 times in a minute is rare); high value (this is exactly
  where credential stuffing happens).
- **Write endpoints** (creating resources): moderate, per-user if
  authenticated — e.g. 10-30/min. Loose enough not to bother normal usage,
  tight enough to blunt scripted abuse.
- **Read endpoints**: loosest, often per-IP only — e.g. 60-120/min. Mostly
  about protecting infrastructure from accidental hammering (a buggy
  client polling too fast), not abuse.

Whatever you pick, treat it as a hypothesis to validate against real
traffic, not a one-time decision.

## Testing your integration

### Unit-testing registration without a real server

`RateLimiter.protect()`/`protect_many()` just call `app.add_middleware(...)`
— a fake `app` that records the call is enough to test your own wiring:

```python
class FakeApp:
    def __init__(self):
        self.registered = []

    def add_middleware(self, cls, **kwargs):
        self.registered.append((cls, kwargs))

app = FakeApp()
limiter.protect_many(app, path="/login", limits=AUTH_POLICY)
assert len(app.registered) == 2
```

See `tests/test_limiter.py` for the full pattern this package's own tests
use.

### Live-testing with curl

Run [`examples/demo_app.py`](../examples/demo_app.py) and hammer an
endpoint the same way its own verification did:

```bash
python examples/demo_app.py &
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "attempt $i: %{http_code}\n" \
    -X POST http://localhost:8090/login \
    -H "Content-Type: application/json" \
    -d '{"username":"victim","password":"wrong"}'
done
```

## Troubleshooting

**My rate limit didn't trigger at all.** Check `path` matches exactly
(including trailing slashes) and `method` matches the route's actual HTTP
method — a mismatch means the middleware silently passes every request
through (`scope.get("path") != self.path` falls through to `await
self.app(...)`, no error).

**Two limits I registered separately seem to be interfering with each
other.** This was a real bug, fixed — every `.protect()`/`.dependency()`
call gets an automatically-unique `name` unless you pass one explicitly.
If you *are* passing explicit `name=` values, check you didn't
accidentally reuse the same one across unrelated limits.

**My limit is looser than I configured, behind a load balancer.**
`InMemoryStore` is per-process — each worker/replica has its own counters,
so N workers means the effective limit is `configured_limit * N`. Not
fixable until a shared store (Redis) ships; see DESIGN.md's build order.

**`by_ip` always returns `"ip:unknown"`.** In dependency mode, this reads
`request._scope["client"]` — a private Jetio attribute that may not be
populated in every test harness or deployment shape (e.g. behind certain
proxy configurations without `client` in the ASGI scope). Verify with a
real running server, not a mocked request.
