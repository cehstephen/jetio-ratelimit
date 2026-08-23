"""Scenario app 2: a public, unauthenticated data API -- no jetio-auth at
all, proving jetio-ratelimit stands on its own. One free tier (IP-keyed,
tight) and one partner tier (API-key-keyed via a header, looser). Used by
tests_e2e/test_public_api_scenarios.py.
"""

import os

from jetio import Jetio, Request, JsonResponse, Depends, add_swagger_ui
from jetio_ratelimit import RateLimiter, InMemoryStore, by_header, by_ip

app = Jetio(title="Public API scenario")
add_swagger_ui(app)

limiter = RateLimiter(store=InMemoryStore())

CATALOG = [{"id": 1, "name": "widget"}, {"id": 2, "name": "gadget"}]

# Free tier: no API key required, tight IP-based limit.
free_tier_limit = limiter.dependency(max_attempts=3, window_seconds=60, key_func=by_ip)

# Partner tier: requires an API key, keyed by that key rather than IP (so
# a partner behind a shared corporate NAT isn't penalized for sharing an
# IP with other partners, and a caller can't dodge the limit by rotating
# source IPs while reusing the same key).
partner_tier_limit = limiter.dependency(max_attempts=10, window_seconds=60, key_func=by_header("x-api-key"))


@app.route("/catalog/free", methods=["GET"])
async def catalog_free(request: Request, ok=Depends(free_tier_limit)):
    return JsonResponse(CATALOG)


@app.route("/catalog/partner", methods=["GET"])
async def catalog_partner(request: Request, ok=Depends(partner_tier_limit)):
    if not request.headers.get("x-api-key"):
        return JsonResponse({"error": "x-api-key header required for partner tier"}, status_code=401)
    return JsonResponse(CATALOG)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ["JETIO_APP_PORT"]))
