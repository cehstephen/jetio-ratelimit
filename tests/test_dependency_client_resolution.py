"""_resolve_client_ip() bridges two jetio versions: the one currently on
PyPI (1.2.2, Request has no public .client) and the fixed one in
cehstephen/jetio#4 (Request.client is public), which isn't merged/published
yet. Both branches need to keep working until that fix ships and this
package's minimum jetio version is bumped past it -- see the function's
docstring in jetio_ratelimit/dependency.py.
"""

from jetio_ratelimit.dependency import _resolve_client_ip


class FixedJetioRequest:
    """Stands in for jetio.Request once cehstephen/jetio#4 is merged."""

    def __init__(self, client):
        self.client = client


class CurrentJetioRequest:
    """Stands in for jetio.Request as currently published (1.2.2) -- no
    public .client, only the private _scope it's built from."""

    def __init__(self, client):
        self._scope = {"client": client}


def test_prefers_the_public_client_attribute_when_present():
    request = FixedJetioRequest(client=("9.9.9.9", 12345))
    assert _resolve_client_ip(request) == "9.9.9.9"


def test_falls_back_to_private_scope_on_current_jetio():
    request = CurrentJetioRequest(client=("8.8.8.8", 54321))
    assert _resolve_client_ip(request) == "8.8.8.8"


def test_returns_none_when_neither_source_has_a_client():
    request = FixedJetioRequest(client=None)
    assert _resolve_client_ip(request) is None

    request = CurrentJetioRequest(client=None)
    assert _resolve_client_ip(request) is None
