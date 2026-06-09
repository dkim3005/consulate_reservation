"""HMAC-signed cookie auth.

Roles: 'admin' (full access + sees the daily passcode) and 'staff'.
Cookie format: `<role>.<issued_at>.<hmac_sha256>` — stateless, server-verifiable.
"""
from __future__ import annotations

import hmac
import time
from hashlib import sha256

from app.config import SESSION_MAX_AGE, SESSION_SECRET

ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
COOKIE_NAME = "consulate_auth"


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), sha256).hexdigest()


def make_cookie(role: str) -> str:
    if role not in (ROLE_ADMIN, ROLE_STAFF):
        raise ValueError(f"invalid role: {role!r}")
    payload = f"{role}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def verify_cookie(value: str | None) -> str | None:
    if not value:
        return None
    try:
        role, ts_str, sig = value.rsplit(".", 2)
    except ValueError:
        return None
    if role not in (ROLE_ADMIN, ROLE_STAFF):
        return None
    expected = _sign(f"{role}.{ts_str}")
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        issued = int(ts_str)
    except ValueError:
        return None
    if time.time() - issued > SESSION_MAX_AGE:
        return None
    return role
