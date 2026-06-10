"""HMAC-signed cookie auth.

Roles: 'admin' (full access + sees the daily passcode) and 'staff'.

Cookie format: `<role>.<target_date>.<issued_at>.<hmac_sha256>`
  * `target_date` (YYYY-MM-DD) binds the session to a specific business day.
  * When the dashboard rolls to the next day (BUSINESS_END), the verifier
    rejects the cookie and the user is forced to re-login. This implements the
    "session lasts one business day" semantics the user asked for.
  * `SESSION_MAX_AGE` is still enforced as an upper bound safety net.
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


def make_cookie(role: str, target_date: str) -> str:
    if role not in (ROLE_ADMIN, ROLE_STAFF):
        raise ValueError(f"invalid role: {role!r}")
    payload = f"{role}.{target_date}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def verify_cookie(value: str | None, current_target_date: str | None = None) -> str | None:
    """Return the role if the cookie is valid, else None.

    If `current_target_date` is given, the cookie's embedded target_date must
    match it — sessions issued for a previous business day are rejected.
    """
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 4:
        return None
    role, target_date, ts_str, sig = parts
    if role not in (ROLE_ADMIN, ROLE_STAFF):
        return None
    expected = _sign(f"{role}.{target_date}.{ts_str}")
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        issued = int(ts_str)
    except ValueError:
        return None
    if time.time() - issued > SESSION_MAX_AGE:
        return None
    if current_target_date is not None and target_date != current_target_date:
        return None
    return role
