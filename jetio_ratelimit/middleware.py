"""ASGI middleware mode: protects a whole route + method, including routes
we don't own the handler for (e.g. jetio-auth's AuthRouter registers
/login and /register internally -- there's no Depends() hook to attach to
on those, so middleware is the only way to rate-limit them).
"""

import json

from jetio import BaseMiddleware, JsonResponse

from .keys import KeyContext, KeyFunc, by_ip
from .stores import RateLimitStore


class RateLimitMiddleware(BaseMiddleware):
    """Rate-limits one path+method. Register multiple instances (via
    `app.add_middleware`) to stack independent limits -- e.g. one by_ip and
    one by_field("username") on /login, so either tripping blocks the
    request. That combination is what actually resists credential
    stuffing: distributed-IP attacks trip the account-keyed limit even
    though no single IP trips the IP-keyed one.
    """

    def __init__(
        self,
        app,
        store: RateLimitStore,
        name: str,
        path: str,
        max_attempts: int,
        window_seconds: float = 60,
        method: str = "POST",
        key_func: KeyFunc = by_ip,
    ):
        super().__init__(app)
        self.store = store
        # Namespaces this limit's store keys so two independently-registered
        # limits can never collide even if key_func produces the same raw
        # string for both -- e.g. two limits stacked on the same route (by
        # design: IP-keyed + account-keyed on /login both target POST/login).
        # Without this, InMemoryStore's dict is keyed purely by that string,
        # so a looser limit registered elsewhere under the same key would
        # silently borrow -- or donate -- hits to the other. `name` is
        # assigned centrally by RateLimiter, which is the one place that
        # knows about every limit registered from the same store.
        self.name = name
        self.path = path
        self.method = method.upper()
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.key_func = key_func

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != self.path or scope.get("method") != self.method:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else None

        # Raw ASGI headers are a list of (bytes, bytes) pairs; decode into a
        # lowercase-keyed dict so by_header() lookups are case-insensitive
        # regardless of how the client capitalized the header name.
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        # Buffer the body so key_func can read it (e.g. by_field("username")),
        # then replay it to the downstream app via a wrapped `receive` --
        # ASGI request bodies are a one-shot stream, so anyone downstream
        # who tries to read it again without this would get nothing.
        body_chunks = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                break
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)

        try:
            parsed_body = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            parsed_body = {}

        ctx = KeyContext(ip=ip, body=parsed_body, headers=headers)
        key = f"{self.name}:{self.key_func(ctx)}"

        result = await self.store.hit(key, self.max_attempts, self.window_seconds)

        if not result.allowed:
            response = JsonResponse(
                {"error": "Too many attempts, try again later."},
                status_code=429,
                headers={"Retry-After": str(result.retry_after_seconds)},
            )
            await response(scope, receive, send)
            return

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
