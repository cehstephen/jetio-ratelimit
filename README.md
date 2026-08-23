# jetio-ratelimit

Rate limiting for [Jetio](https://pypi.org/project/jetio/): sliding-window
by default, IP- or account-keyed (or both, stacked), usable as middleware
for routes you don't own the handler for (like jetio-auth's `/login`) and
as a `Depends()`-composable dependency for routes you do (a `CrudRouter`
policy, a hand-written route).

See [DESIGN.md](DESIGN.md) for the full reasoning -- why sliding window
over token bucket, why IP-only limiting is weak against real credential
stuffing, and the real bugs found in Jetio/jetio-auth along the way.

## Install

```
pip install -e .[dev]   # from this directory, for now -- not yet published
```

## Quickstart

```python
from jetio import Jetio, CrudRouter
from jetio_auth import AuthRouter
from jetio_ratelimit import RateLimiter, InMemoryStore, Limit, by_ip, by_field, by_user

app = Jetio()
auth = AuthRouter(User, company_name="My App")
auth.register_routes(app)  # POST /register, POST /login

limiter = RateLimiter(store=InMemoryStore())

# Middleware mode: protects /login, which AuthRouter registers internally --
# there's no Depends() hook to attach to on a route we don't define.
# protect_many() stacks two independent limits in one call; either tripping
# blocks the request. AUTH_POLICY is a plain list, so the same two rules
# can be applied to /register or any other auth-adjacent route with one
# more protect_many() call each -- see "Reusing a policy across routes" below.
AUTH_POLICY = [
    Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
    Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
]
limiter.protect_many(app, path="/login", limits=AUTH_POLICY)

# Dependency mode: composes into a CrudRouter policy, keyed by the
# authenticated user rather than IP.
CrudRouter(
    model=Order,
    secure=True,
    policy={
        "POST": limiter.dependency(
            max_attempts=10, window_seconds=60,
            key_func=by_user, identity_dependency=auth.get_auth_dependency(),
        ),
    },
).register_routes(app)
```

Run [examples/demo_app.py](examples/demo_app.py) and hit it with curl to see
both modes working against a real jetio-auth-backed app.

## Why two limits on `/login`, not one

Real credential-stuffing tools spread attempts across many IPs specifically
to dodge IP-based rate limits. An IP-keyed limit alone catches noisy,
single-source abuse; it does little against a botnet making 1-2 attempts
per IP. Stack an account-keyed limit (`by_field("username")`) alongside it
-- whichever trips first blocks the request -- and a distributed attack
against one account still gets caught.

## Reusing a policy across routes

`.protect()` (one limit per call) still works exactly as before -- nothing
about it changed. `.protect_many()` is purely additive: pass a list of
`Limit`s and it registers each one, so a route needing 2 stacked rules goes
from 2 calls to 1. The bigger payoff is that `limits` is a plain Python
list, reusable across every route that should share one policy:

```python
for path in ["/login", "/register", "/reset-password"]:
    limiter.protect_many(app, path=path, limits=AUTH_POLICY)
```

Without this, 30 endpoints needing the same 2-rule policy is 60
near-identical `.protect()` calls -- tedious, and a typo'd limit on one
route silently drifts out of sync with the rest. With a shared `Limit`
list, changing the policy means editing it in one place.

## Status

v0.1: sliding window algorithm, in-memory store, both API modes, IP/account/
user keying, `protect_many()` for stacking multiple limits (or reusing one
policy across many routes) in one call. Not yet done: Redis store (for
anything running more than one worker -- InMemoryStore's state is
per-process), progressive lockout on repeat violations, an equivalent
stacking helper for dependency mode, PyPI publish. See DESIGN.md's build
order.

## Known limitations

- **InMemoryStore is single-process.** Behind multiple workers/replicas,
  each one has its own counters, so the effective limit becomes
  `limit * worker_count`. Don't treat this as sufficient for a
  horizontally-scaled deployment yet.
- **`Retry-After` header only in middleware mode.** Dependency mode raises
  `starlette.exceptions.HTTPException(429)`, and Jetio core's exception
  handler currently discards `.headers` on the way out (confirmed against
  jetio 1.2.2 -- it only reads `.detail`/`.status_code`). The retry time is
  embedded in the error message text instead. Worth a real fix upstream.
- **`by_ip` reads a private attribute** (`request._scope["client"]`) in
  dependency mode, since Jetio's `Request` doesn't expose the client IP
  publicly. Fragile by nature of being private API -- worth Jetio core
  exposing `request.client` for this and similar use cases (logging, audit
  trails).
- **No `X-Forwarded-For` support.** Behind a reverse proxy, `by_ip` sees the
  proxy's IP, not the real client's. Not yet configurable.
