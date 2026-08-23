"""Real-life scenarios against a real running SaaS-style app (see
apps/saas_scenario_app.py) -- actual subprocess, actual HTTP, actual
sqlite-backed state. Complements the unit tests in tests/; those prove the
mechanism in isolation, these prove it holds up as a deployed app.
"""

import httpx


def _register(base_url, username, password, email=None):
    return httpx.post(
        f"{base_url}/register",
        json={"username": username, "email": email or f"{username}@example.com", "password": password},
    )


def _login(base_url, username, password):
    return httpx.post(f"{base_url}/login", json={"username": username, "password": password})


def _token(base_url, username, password):
    resp = _login(base_url, username, password)
    return resp.json()["access_token"]


class TestCredentialStuffingResistance:
    """The headline scenario: an attacker who doesn't control a single IP
    (real credential stuffing sprays attempts across many source
    addresses) still gets caught, because the account being targeted
    doesn't change even when the attacker's apparent IP does."""

    def test_account_keyed_limit_trips_before_ip_limit_on_a_targeted_account(self, saas_app):
        _register(saas_app, "victim", "correct-horse-battery-staple")

        # Account limit is 3/min, IP limit is 5/min -- targeting one
        # account should trip the tighter, account-scoped limit first.
        statuses = [_login(saas_app, "victim", "wrong-password").status_code for _ in range(3)]
        assert statuses == [401, 401, 401], "first 3 wrong attempts against the real account should just be 401s"

        blocked = _login(saas_app, "victim", "wrong-password")
        assert blocked.status_code == 429, "4th attempt against the same account must be blocked"
        # /login runs in middleware mode, which builds the 429 response
        # directly rather than raising through Jetio's HTTPException path
        # -- a real Retry-After header, unlike dependency-mode routes (see
        # test_public_api_scenarios.py's free-tier test for that contrast).
        assert "retry-after" in {k.lower() for k in blocked.headers}

    def test_a_different_account_from_the_same_source_is_not_collateral_damage(self, saas_app):
        _register(saas_app, "victim2", "pw")
        _register(saas_app, "bystander", "pw")

        for _ in range(3):
            _login(saas_app, "victim2", "wrong")
        assert _login(saas_app, "victim2", "wrong").status_code == 429

        # bystander's account has its own budget -- an attack on victim2
        # must not lock bystander out too.
        untouched = _login(saas_app, "bystander", "also-wrong")
        assert untouched.status_code == 401, "bystander should get a normal auth failure, not 429"

    def test_broad_low_and_slow_scanning_across_many_accounts_still_trips_the_ip_limit(self, saas_app):
        # A scanner trying one guess each against many different usernames
        # never trips any single account's 3/min limit, but it's still
        # hammering /login from one source -- the IP-keyed limit (5/min)
        # exists precisely to catch this pattern.
        for i in range(5):
            _register(saas_app, f"target{i}", "pw")
        for i in range(5):
            resp = _login(saas_app, f"target{i}", "guess")
            assert resp.status_code == 401, f"attempt {i} should not itself be rate-limited yet"

        blocked = _login(saas_app, "target0", "guess")  # 6th /login call from this process
        assert blocked.status_code == 429, "IP-keyed limit should have caught the breadth of this pattern"


class TestPerUserResourceQuotas:
    def test_order_creation_quota_is_isolated_per_authenticated_user(self, saas_app):
        _register(saas_app, "alice", "pw")
        _register(saas_app, "bob", "pw")
        alice_token = _token(saas_app, "alice", "pw")
        bob_token = _token(saas_app, "bob", "pw")

        def create_order(token):
            return httpx.post(
                f"{saas_app}/orders",
                headers={"Authorization": f"Bearer {token}"},
                json={"item": "widget", "total": 9.99, "user_id": 1},
            )

        statuses = [create_order(alice_token).status_code for _ in range(3)]
        assert statuses == [200, 200, 200], "alice's first 3 orders (her quota) should succeed"
        assert create_order(alice_token).status_code == 429, "alice's 4th order should be throttled"

        # bob has never created an order -- alice hitting her limit must
        # not have consumed any of bob's separate quota.
        assert create_order(bob_token).status_code == 200, "bob's quota must be untouched by alice's usage"


class TestAdminPromotionFlow:
    def test_only_an_existing_admin_can_promote_another_user(self, saas_app):
        _register(saas_app, "normie", "pw")
        admin_token = _token(saas_app, "admin", "scenario-admin-pw")
        normie_token = _token(saas_app, "normie", "pw")

        users = httpx.get(f"{saas_app}/users/2", headers={"Authorization": f"Bearer {normie_token}"})
        normie_id = users.json()["id"]

        self_promote = httpx.post(
            f"{saas_app}/admin/{normie_id}/make-admin",
            headers={"Authorization": f"Bearer {normie_token}"},
        )
        assert self_promote.status_code == 403, "a normal user must not be able to promote themselves"

        promote = httpx.post(
            f"{saas_app}/admin/{normie_id}/make-admin",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert promote.status_code == 200

        refreshed_token = _token(saas_app, "normie", "pw")
        promoted = httpx.get(f"{saas_app}/users/{normie_id}", headers={"Authorization": f"Bearer {refreshed_token}"})
        assert promoted.json()["is_admin"] is True


class TestSensitiveDataNeverLeaksOverTheWire:
    def test_hashed_password_is_absent_from_the_actual_http_response_body(self, saas_app):
        # Wire-level proof, not schema introspection: read the real JSON
        # bytes a real HTTP client received.
        _register(saas_app, "carol", "hunter2")
        token = _token(saas_app, "carol", "hunter2")

        # carol is the first regular registrant on a fresh app instance
        # (the bootstrapped admin from ensure_admin() takes id 1), so she
        # owns id 2 -- fetch her own record, not an arbitrary one, since
        # owner_or_admin() would (correctly) 403 a non-admin reading
        # someone else's.
        resp = httpx.get(f"{saas_app}/users/2", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "hashed_password" not in resp.text
        assert "hunter2" not in resp.text
