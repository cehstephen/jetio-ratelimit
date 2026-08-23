"""Live integration example: jetio + jetio-auth + jetio-ratelimit together.

Not a unit test -- run it and hit it with curl/httpx to prove out real
behavior, same way the rest of this design was verified. See
../DESIGN.md's build order, step 2/3.
"""

from jetio import Jetio, CrudRouter, JetioModel, add_swagger_ui, Base, engine, SessionLocal
from jetio_auth import AuthRouter
from jetio_ratelimit import RateLimiter, InMemoryStore, Limit, by_ip, by_field, by_user
from sqlalchemy.orm import Mapped, mapped_column


class User(JetioModel):
    class API:
        exclude_from_read = ["hashed_password"]

    username: Mapped[str] = mapped_column(unique=True)
    email: Mapped[str]
    hashed_password: Mapped[str]
    is_admin: Mapped[bool] = mapped_column(default=False)


class Order(JetioModel):
    item: Mapped[str]
    total: Mapped[float]
    user_id: Mapped[int]


app = Jetio(title="jetio-ratelimit demo")
add_swagger_ui(app)

auth = AuthRouter(User, company_name="Demo")
auth.register_routes(app)  # POST /register, POST /login

limiter = RateLimiter(store=InMemoryStore())

# Stacked limits on /login: IP-keyed (stops noisy single-source brute force)
# AND account-keyed (stops distributed credential stuffing against one
# account). Both must pass; either tripping blocks the request. One call via
# protect_many() instead of two separate .protect() calls -- and AUTH_POLICY
# is a plain list, so the same two rules could be applied to /register or any
# other auth-adjacent route with one more protect_many() call, not two more
# .protect() calls.
AUTH_POLICY = [
    Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
    Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
]
limiter.protect_many(app, path="/login", limits=AUTH_POLICY)

# Dependency mode: rate-limit order creation per authenticated user,
# composed with jetio-auth's own dependency inside a CrudRouter policy.
CrudRouter(
    model=Order,
    secure=True,
    policy={
        "GET": auth.owner_or_admin(Order),
        "POST": limiter.dependency(
            max_attempts=3,
            window_seconds=60,
            key_func=by_user,
            identity_dependency=auth.get_auth_dependency(),
        ),
        "PUT": auth.owner_or_admin(Order),
        "DELETE": auth.owner_or_admin(Order),
    },
).register_routes(app)


@app.on_event("startup")
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        await auth.ensure_admin(db, username="admin", password="change-me-now", email="admin@demo.local")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
