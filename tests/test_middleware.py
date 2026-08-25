"""Direct ASGI-level tests for RateLimitMiddleware's edge cases -- the
paths tests_e2e's real scenario apps never happen to hit (well-formed
JSON, uninterrupted request bodies), tested here without needing a live
server since __call__ is a plain ASGI callable.
"""

import pytest

from jetio_ratelimit.keys import by_field
from jetio_ratelimit.middleware import RateLimitMiddleware
from jetio_ratelimit.stores import InMemoryStore


async def _downstream_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _make_middleware(**overrides):
    kwargs = dict(
        app=_downstream_app,
        store=InMemoryStore(),
        name="test",
        path="/login",
        max_attempts=5,
        window_seconds=60,
        method="POST",
        key_func=by_field("username"),
    )
    kwargs.update(overrides)
    return RateLimitMiddleware(**kwargs)


def _scope():
    return {"type": "http", "method": "POST", "path": "/login", "headers": [], "client": ("127.0.0.1", 12345)}


@pytest.mark.asyncio
async def test_malformed_json_body_is_treated_as_empty_rather_than_crashing():
    middleware = _make_middleware()

    async def receive():
        return {"type": "http.request", "body": b"not-json{{{", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await middleware(_scope(), receive, send)

    # by_field("username") sees an empty body -> "username:missing", not a
    # KeyError/crash, and the request still reaches the downstream app.
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_body_read_stops_cleanly_on_a_non_request_message():
    middleware = _make_middleware()

    messages = iter(
        [
            {"type": "http.request", "body": b'{"username":', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    sent = []

    async def send(message):
        sent.append(message)

    await middleware(_scope(), receive, send)

    # The partial (invalid-JSON) body collected before the disconnect is
    # treated as empty, same as the malformed-body case above.
    assert sent[0]["status"] == 200
