"""_resolve_client_ip() reads jetio.Request.client, public as of jetio
1.2.3 (this package's minimum version, cehstephen/jetio#4). Its own tiny
function mainly so it's testable without constructing a real Request.
"""

from jetio_ratelimit.dependency import _resolve_client_ip


class FakeRequest:
    def __init__(self, client):
        self.client = client


def test_reads_the_public_client_attribute():
    request = FakeRequest(client=("9.9.9.9", 12345))
    assert _resolve_client_ip(request) == "9.9.9.9"


def test_returns_none_when_there_is_no_client():
    request = FakeRequest(client=None)
    assert _resolve_client_ip(request) is None
