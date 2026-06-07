"""
NuroQ auth — single-user password login with signed session cookies.

The cloud box (https://nuroq.nuroquant.com) is publicly reachable, so SOMETHING
has to gate /api/*. Historically that was a 48-char `X-NuroQ-Key` shared in a
URL — strong secret, painful UX (per-origin cookies, 24h expiry). This module
replaces it with a familiar password login: paste the password once → 30-day
session cookie → no more re-auth.

Storage: `auth_settings` row in the existing SQLite DB (NUROQ_DB_PATH). Seeded
on first call with `INITIAL_PASSWORD = "nuroq"` — the user is expected to change
it immediately via the in-app form. The seed is intentionally weak so the user
isn't locked out; in change it.

Hashing: PBKDF2-HMAC-SHA256, 600k iterations (OWASP-recommended for sha256),
16-byte random salt. Stdlib only — no bcrypt/argon2 dep needed.

Session token: `base64url(json({iat, exp})) + "." + base64url(HMAC-SHA256(body))`.
Signed with a per-box random `session_secret` persisted in the same DB row, so
sessions survive restarts but are unique per deploy / per environment. Verified
in constant time with `hmac.compare_digest`.

This module is intentionally self-contained — no FastAPI imports — so the
middleware in `backend/api.py` calls `verify_token()` directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

_TABLE = """
CREATE TABLE IF NOT EXISTS auth_settings (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  password_hash  TEXT NOT NULL,
  salt           TEXT NOT NULL,
  iters          INTEGER NOT NULL,
  session_secret TEXT NOT NULL,
  updated_at     INTEGER NOT NULL
);
"""

PBKDF2_ITERS = 600_000
INITIAL_PASSWORD = "nuroq"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
COOKIE_NAME = "nuroq_session"


def _db_path() -> str:
    return os.environ.get("NUROQ_DB_PATH", "nuroq.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.execute(_TABLE)
    return c


def _hash(password: str, salt: bytes, iters: int = PBKDF2_ITERS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)


def _seed_if_empty() -> None:
    """Create the singleton row on first run. Idempotent — safe to call often."""
    with _conn() as c:
        if c.execute("SELECT 1 FROM auth_settings WHERE id=1").fetchone():
            return
        salt = secrets.token_bytes(16)
        h = _hash(INITIAL_PASSWORD, salt)
        c.execute(
            "INSERT INTO auth_settings(id, password_hash, salt, iters, "
            "session_secret, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
            (h.hex(), salt.hex(), PBKDF2_ITERS,
             secrets.token_hex(32), int(time.time())),
        )


def verify_password(password: str) -> bool:
    """Constant-time check against the stored hash. Seeds on first call."""
    _seed_if_empty()
    with _conn() as c:
        row = c.execute(
            "SELECT password_hash, salt, iters FROM auth_settings WHERE id=1"
        ).fetchone()
    expected_hex, salt_hex, iters = row
    candidate = _hash(password, bytes.fromhex(salt_hex), iters).hex()
    return hmac.compare_digest(candidate, expected_hex)


def change_password(new_password: str) -> None:
    """Replace the stored hash. Caller is responsible for verifying the OLD one."""
    _seed_if_empty()
    salt = secrets.token_bytes(16)
    h = _hash(new_password, salt)
    with _conn() as c:
        c.execute(
            "UPDATE auth_settings SET password_hash=?, salt=?, iters=?, "
            "updated_at=? WHERE id=1",
            (h.hex(), salt.hex(), PBKDF2_ITERS, int(time.time())),
        )


def _session_secret() -> bytes:
    _seed_if_empty()
    with _conn() as c:
        row = c.execute(
            "SELECT session_secret FROM auth_settings WHERE id=1"
        ).fetchone()
    return bytes.fromhex(row[0])


def _b64e(b: bytes) -> bytes:
    return urlsafe_b64encode(b).rstrip(b"=")


def _b64d(b: bytes) -> bytes:
    return urlsafe_b64decode(b + b"=" * (-len(b) % 4))


def issue_token(now: int | None = None) -> str:
    """Mint a fresh session token (a signed `body.signature` string)."""
    t = int(now if now is not None else time.time())
    body = _b64e(json.dumps({"iat": t, "exp": t + SESSION_TTL},
                            separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_session_secret(), body, hashlib.sha256).digest())
    return (body + b"." + sig).decode()


def verify_token(token: str | None) -> bool:
    """True iff the token is well-formed, correctly signed, and unexpired."""
    if not token or "." not in token:
        return False
    body, sig = token.encode().split(b".", 1)
    expected = _b64e(hmac.new(_session_secret(), body, hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(_b64d(body).decode())
    except Exception:
        return False
    return int(payload.get("exp", 0)) > int(time.time())
