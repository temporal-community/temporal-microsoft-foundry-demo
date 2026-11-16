"""Access-code login with a signed cookie, and small in-memory rate limits.

The console is a public page at a conference. The access code keeps passers-by on the
internet from spending model tokens; it is not user authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

COOKIE_NAME = "tfd_session"
SESSION_SECONDS = 12 * 3600


def codes_match(given: str, expected: str) -> bool:
    """Constant-time comparison, so response timing says nothing about the code."""
    return hmac.compare_digest(given.encode(), expected.encode())


def _signature(secret: bytes, body: str) -> str:
    return hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()


def make_cookie(secret: bytes, *, now: float | None = None, ttl: int = SESSION_SECONDS) -> str:
    session_id = secrets.token_hex(12)
    expires = int((now if now is not None else time.time()) + ttl)
    body = f"v1.{session_id}.{expires}"
    return f"{body}.{_signature(secret, body)}"


def read_cookie(secret: bytes, value: str | None, *, now: float | None = None) -> str | None:
    """Return the session ID of a valid, unexpired cookie, else None."""
    if not value:
        return None
    body, _, signature = value.rpartition(".")
    parts = body.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    if not hmac.compare_digest(signature, _signature(secret, body)):
        return None
    try:
        expires = int(parts[2])
    except ValueError:
        return None
    if expires < (now if now is not None else time.time()):
        return None
    return parts[1]


class RateLimiter:
    """At most ``limit`` events per ``window`` seconds for each key."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        events = self._events[key]
        while events and now - events[0] >= self._window:
            events.popleft()
        return events

    def full(self, key: str, *, now: float | None = None) -> bool:
        """True when the key has used its allowance; does not count as an event."""
        return len(self._recent(key, now if now is not None else time.monotonic())) >= self._limit

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Count one event for the key, unless it has used its allowance."""
        now = now if now is not None else time.monotonic()
        events = self._recent(key, now)
        if len(events) >= self._limit:
            return False
        events.append(now)
        if len(self._events) > 10_000:  # a scan of the access code must not grow this forever
            for stale in [k for k, v in self._events.items() if not v or now - v[-1] >= self._window]:
                del self._events[stale]
        return True
