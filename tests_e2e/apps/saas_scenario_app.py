"""Scenario app 1: a small SaaS API -- auth, per-account resources, and an
admin flow. Combines jetio + jetio-auth + jetio-ratelimit the way a real
product would, not a minimal toy. Used by tests_e2e/test_saas_scenarios.py.
"""

import os

import _coverage_shutdown
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


app = Jetio(title="SaaS scenario")
add_swagger_ui(app)

auth = AuthRouter(User, company_name="Scenario SaaS")
auth.register_routes(app)  # POST /register, POST /login
auth.register_admin_routes(app)  # POST /admin/{id}/make-admin

limiter = RateLimiter(store=InMemoryStore())

# Stacked login protection: IP-keyed catches noisy single-source brute
# force, account-keyed catches distributed credential stuffing against one
# target account.
AUTH_POLICY = [
    Limit(max_attempts=5, window_seconds=60, key_func=by_ip),
    Limit(max_attempts=3, window_seconds=60, key_func=by_field("username")),
]
limiter.protect_many(app, path="/login", limits=AUTH_POLICY)
limiter.protect_many(app, path="/register", limits=AUTH_POLICY)

CrudRouter(
    model=User,
    exclude_methods=["POST"],
    secure=True,
    policy={
        "GET": auth.owner_or_admin(User, audit_fields=["id"]),
        "PUT": auth.owner_or_admin(User, audit_fields=["id"]),
        "DELETE": auth.admin_only(),
    },
).register_routes(app)

CrudRouter(
    model=Order,
    secure=True,
    policy={
        "GET": auth.owner_or_admin(Order),
        # Per-user creation quota -- deliberately tight (3/min) so the
        # scenario test can trip it without hundreds of requests.
        "POST": limiter.dependency(
            max_attempts=3, window_seconds=60,
            key_func=by_user, identity_dependency=auth.get_auth_dependency(),
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
        await auth.ensure_admin(
            db,
            username=os.environ.get("ADMIN_USERNAME", "admin"),
            password=os.environ.get("ADMIN_PASSWORD", "scenario-admin-pw"),
            email="admin@scenario.local",
        )


if __name__ == "__main__":
    _coverage_shutdown.install()
    app.run(host="127.0.0.1", port=int(os.environ["JETIO_APP_PORT"]))
