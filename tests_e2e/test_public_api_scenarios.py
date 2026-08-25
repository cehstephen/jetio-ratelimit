"""Real-life scenarios against a real running public API (see
apps/public_api_scenario_app.py) that uses jetio-ratelimit with no
jetio-auth at all -- proving the plugin doesn't require an auth layer to
be useful, and that header-keyed (API key) limiting behaves independently
of IP-keyed limiting.
"""

import httpx


class TestFreeTierIPLimiting:
    def test_free_tier_blocks_after_its_threshold(self, public_api_app):
        statuses = [httpx.get(f"{public_api_app}/catalog/free").status_code for _ in range(3)]
        assert statuses == [200, 200, 200]

        blocked = httpx.get(f"{public_api_app}/catalog/free")
        assert blocked.status_code == 429
        # /catalog/free uses dependency mode (Depends(), not middleware),
        # which raises through Jetio's HTTPException path. As of jetio 1.2.3
        # (this package's minimum version), that path propagates
        # HTTPException.headers, so a real Retry-After reaches the client
        # here too -- same as middleware-mode routes (e.g. /login in the
        # SaaS scenario, see test_saas_scenarios.py).
        assert blocked.headers.get("retry-after") is not None
        assert "retry after" in blocked.json()["detail"]


class TestPartnerTierApiKeyLimiting:
    def test_two_api_keys_from_the_same_process_get_independent_quotas(self, public_api_app):
        def call(key):
            return httpx.get(f"{public_api_app}/catalog/partner", headers={"X-API-Key": key})

        # Partner tier limit is 10/min -- exhaust key A specifically.
        for _ in range(10):
            resp = call("key-A")
            assert resp.status_code == 200

        exhausted = call("key-A")
        assert exhausted.status_code == 429, "key-A should now be throttled"

        # Same process, same source IP, DIFFERENT key -- must be unaffected,
        # proving the limit is keyed by the header, not the caller's IP.
        still_fine = call("key-B")
        assert still_fine.status_code == 200

    def test_missing_api_key_is_rate_limited_and_then_rejected_by_the_route(self, public_api_app):
        # No key: the rate-limit dependency still runs (keyed under its own
        # "missing" bucket) and lets the request through to the handler,
        # which then makes its own, separate decision to reject it. Rate
        # limiting and authorization are independent layers here.
        resp = httpx.get(f"{public_api_app}/catalog/partner")
        assert resp.status_code == 401
        assert "x-api-key" in resp.json()["error"]
