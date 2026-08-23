from jetio_ratelimit.keys import KeyContext, by_field, by_header, by_ip, by_user


def test_by_ip_falls_back_to_unknown():
    assert by_ip(KeyContext(ip=None)) == "ip:unknown"
    assert by_ip(KeyContext(ip="1.2.3.4")) == "ip:1.2.3.4"


def test_by_field_reads_body():
    key_func = by_field("username")
    assert key_func(KeyContext(ip=None, body={"username": "alice"})) == "username:alice"
    assert key_func(KeyContext(ip=None, body={})) == "username:missing"


def test_by_header_is_case_insensitive_by_construction():
    # by_header lowercases the header name it's given; callers are expected
    # to populate ctx.headers with lowercase keys too (both middleware.py
    # and dependency.py do this), so a mismatched-case lookup still works.
    key_func = by_header("X-API-Key")
    ctx = KeyContext(ip=None, headers={"x-api-key": "secret123"})
    assert key_func(ctx) == "x-api-key:secret123"


def test_by_header_missing():
    key_func = by_header("x-api-key")
    assert key_func(KeyContext(ip=None)) == "x-api-key:missing"


def test_by_user_uses_id_or_anonymous():
    class FakeUser:
        id = 42

    assert by_user(KeyContext(ip=None, user=FakeUser())) == "user:42"
    assert by_user(KeyContext(ip=None, user=None)) == "user:anonymous"
