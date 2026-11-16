"""Login cookie and rate limits."""

from __future__ import annotations

from app import auth

SECRET = b"test-secret"


def test_cookie_round_trip_and_expiry() -> None:
    cookie = auth.make_cookie(SECRET, now=1000.0, ttl=60)
    session_id = auth.read_cookie(SECRET, cookie, now=1030.0)
    assert session_id and len(session_id) == 24
    assert auth.read_cookie(SECRET, cookie, now=1061.0) is None


def test_cookie_rejects_tampering_and_other_secrets() -> None:
    cookie = auth.make_cookie(SECRET, now=1000.0, ttl=60)
    version, session_id, expires, signature = cookie.split(".")
    longer = f"{version}.{session_id}.{int(expires) + 99999}.{signature}"
    assert auth.read_cookie(SECRET, longer, now=1030.0) is None
    assert auth.read_cookie(b"another", cookie, now=1030.0) is None
    for junk in (None, "", "v1", "a.b.c.d", "v2.x.9999999999.sig", cookie + "x"):
        assert auth.read_cookie(SECRET, junk, now=1030.0) is None


def test_codes_match() -> None:
    assert auth.codes_match("booth-2026", "booth-2026")
    assert not auth.codes_match("booth-2025", "booth-2026")
    assert not auth.codes_match("", "booth-2026")


def test_rate_limiter_is_per_key_and_slides() -> None:
    limiter = auth.RateLimiter(limit=2, window=60)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=1)
    assert not limiter.allow("a", now=2)
    assert limiter.allow("b", now=2)
    assert limiter.allow("a", now=60.5)  # the first event has left the window
    assert not limiter.allow("a", now=60.6)
    assert limiter.full("a", now=60.6) and not limiter.full("b", now=60.6)
    assert not limiter.full("b", now=60.6)  # asking does not count
