"""Who is calling, and what they may see.

Three kinds of caller:
- guest: anyone who opens the site. The page asks for a guest session and keeps its token in memory only (sent as
  the X-GaugeLens-Session header, or ?s= on image and event URLs), so a refresh or closed tab ends it. An ended or
  idle session's clips, frames, readings and chat are deleted (purge.py).
- user: signed in with email + password. The token is an httpOnly cookie; their clips stay across visits.
- admin: a user with role admin. Sees every clip, including guests' (while they exist) and eval clips.

An admin can also mark a clip public: an example every visitor can read, but only its owner or an admin can change.

A clip or equipment row belongs to a user (owner_user_id) or to a guest session (owner_session_id, no user).
Rows with neither (seed, eval, rows from before accounts existed) are visible to admins only.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .ids import new_id

COOKIE = "gaugelens_session"
HEADER = "x-gaugelens-session"
TOUCH_EVERY = timedelta(seconds=60)          # last_seen_at is written at most this often per session
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8

# scrypt: memory-hard, in the standard library (no extra dependency). ~16 MB and ~50 ms per hash.
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(digest) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    kind: str                       # guest | user | admin
    session_id: str
    user_id: str | None = None
    email: str | None = None
    via_cookie: bool = False

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def label(self) -> str:
        return self.email or "guest"

    def owner(self) -> tuple[str | None, str]:
        """(owner_user_id, owner_session_id) for rows this caller creates."""
        return self.user_id, self.session_id

    def scope(self) -> tuple[bool, str | None, str | None]:
        """Parameters for visible(): (is_admin, user_id, guest session_id)."""
        return self.is_admin, self.user_id, None if self.user_id else self.session_id


def visible(alias: str, first: int) -> str:
    """SQL predicate: may the caller see this row? Uses parameters $first..$first+2 = Principal.scope()."""
    a, u, s = f"${first}::bool", f"${first + 1}::text", f"${first + 2}::text"
    return (f"({a} OR ({alias}.owner_user_id IS NOT NULL AND {alias}.owner_user_id = {u}) "
            f"OR ({alias}.owner_user_id IS NULL AND {alias}.owner_session_id = {s}))")


def readable(alias: str, first: int) -> str:
    """Clips: visible() plus examples anyone may read. Anything that changes a clip keeps using visible()."""
    return f"({alias}.is_public OR {visible(alias, first)})"


def catalogue_or_visible(alias: str, first: int) -> str:
    """Equipment: the caller's own, or the shared catalogue (registered by hand or by the seed, never by a clip)."""
    return (f"({visible(alias, first)} OR ({alias}.owner_user_id IS NULL AND {alias}.owner_session_id IS NULL "
            f"AND {alias}.site_id IS DISTINCT FROM 'auto'))")


# --- sessions ---------------------------------------------------------------------------------------------------

async def start_session(pool, kind: str, user_id: str | None = None, days: int | None = None) -> tuple[str, str]:
    """(session_id, token). The token is shown once; only its hash is stored."""
    token, session_id = secrets.token_urlsafe(32), new_id("ses_")
    expires = datetime.now(timezone.utc) + timedelta(days=days) if days else None
    await pool.execute(
        "INSERT INTO sessions (session_id, token_hash, kind, user_id, expires_at) VALUES ($1, $2, $3, $4, $5)",
        session_id, _token_hash(token), kind, user_id, expires)
    return session_id, token


async def end_session(pool, token: str, reason: str) -> str | None:
    """Ends the session this token opened. Returns its id (None if unknown or already ended)."""
    return await pool.fetchval(
        "UPDATE sessions SET ended_at = now(), end_reason = $2 WHERE token_hash = $1 AND ended_at IS NULL "
        "RETURNING session_id", _token_hash(token), reason)


async def principal_for(pool, settings, token: str | None, *, via_cookie: bool) -> Principal | None:
    if not token:
        return None
    row = await pool.fetchrow(
        """SELECT s.session_id, s.kind, s.user_id, s.last_seen_at, s.expires_at, u.email, u.role
           FROM sessions s LEFT JOIN users u USING (user_id)
           WHERE s.token_hash = $1 AND s.ended_at IS NULL""", _token_hash(token))
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if row["kind"] == "guest":
        if via_cookie or now - row["last_seen_at"] > timedelta(minutes=settings.guest_idle_minutes):
            return None                              # guests never ride on a cookie; idle ones are purged
    elif row["expires_at"] is not None and row["expires_at"] < now:
        return None
    if now - row["last_seen_at"] > TOUCH_EVERY:
        expires = now + timedelta(days=settings.user_session_days) if row["kind"] == "user" else None
        await pool.execute("UPDATE sessions SET last_seen_at = now(), expires_at = COALESCE($2, expires_at) "
                           "WHERE session_id = $1", row["session_id"], expires)
    kind = "guest" if row["kind"] == "guest" else ("admin" if row["role"] == "admin" else "user")
    return Principal(kind, row["session_id"], row["user_id"], row["email"], via_cookie)


def _tokens(request: Request) -> tuple[str | None, str | None]:
    """(guest token from header or ?s=, account token from the cookie)."""
    return (request.headers.get(HEADER) or request.query_params.get("s"), request.cookies.get(COOKIE))


async def optional_principal(request: Request) -> Principal | None:
    """The account wins over a guest token sent alongside it."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database not available")
    guest, account = _tokens(request)
    settings = request.app.state.settings
    return (await principal_for(pool, settings, account, via_cookie=True)
            or await principal_for(pool, settings, guest, via_cookie=False))


async def current_principal(request: Request) -> Principal:
    """FastAPI dependency: every data route needs a session (401 tells the page to start a new one)."""
    principal = await optional_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="session ended")
    return principal


async def admin_principal(request: Request) -> Principal:
    principal = await current_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    return principal


async def guest_token_principal(request: Request) -> Principal | None:
    """The guest session sent with a sign-in request, whose clips move to the account."""
    pool, settings = request.app.state.pool, request.app.state.settings
    return await principal_for(pool, settings, _tokens(request)[0], via_cookie=False)


# --- ownership checks -------------------------------------------------------------------------------------------

async def require_clip(pool, principal: Principal, clip_id: str, *, edit: bool = False) -> None:
    """404 (not 403) for clips the caller can't see, so their existence isn't revealed. edit=True: not just read."""
    check = visible("c", 2) if edit else readable("c", 2)
    if not await pool.fetchval(f"SELECT 1 FROM clips c WHERE c.clip_id = $1 AND {check}", clip_id, *principal.scope()):
        raise HTTPException(status_code=404, detail="clip not found")


async def can_edit_clip(pool, principal: Principal, clip_id: str) -> bool:
    return bool(await pool.fetchval(f"SELECT 1 FROM clips c WHERE c.clip_id = $1 AND {visible('c', 2)}",
                                    clip_id, *principal.scope()))


async def require_anomaly(pool, principal: Principal, anomaly_id: str, *, edit: bool = False) -> None:
    check = visible("c", 2) if edit else f"(c.is_public IS TRUE OR {visible('c', 2)})"
    if not await pool.fetchval(
            f"SELECT 1 FROM anomalies a LEFT JOIN clips c USING (clip_id) WHERE a.anomaly_id = $1 AND {check}",
            anomaly_id, *principal.scope()):
        raise HTTPException(status_code=404, detail="anomaly not found")


CLIP_URI = re.compile(r"^clip=([A-Za-z0-9_-]+)/")


async def require_evidence(pool, principal: Principal, uri: str) -> None:
    """Evidence files live under clip={clip_id}/; anything else is admin-only. The URI must be plain: the first folder
    is what gets authorized, so a ".." later on would reach another clip's files with this clip's permission."""
    parts = uri.split("/")
    if "\\" in uri or "\0" in uri or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=400, detail="invalid evidence path")
    match = CLIP_URI.match(uri)
    if match:
        await require_clip(pool, principal, match.group(1))
    elif not principal.is_admin:
        raise HTTPException(status_code=404, detail="evidence not found")


# --- accounts ---------------------------------------------------------------------------------------------------

def normalise_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL.match(email) or len(email) > 254:
        raise ValueError("enter a valid email address")
    return email


async def create_user(pool, email: str, password: str, role: str = "user") -> str:
    email = normalise_email(email)
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters for the password")
    user_id = new_id("usr_")
    row = await pool.fetchval(
        "INSERT INTO users (user_id, email, password_hash, role) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (email) DO NOTHING RETURNING user_id", user_id, email, hash_password(password), role)
    if row is None:
        raise LookupError("an account with this email already exists")
    return user_id


async def authenticate(pool, email: str, password: str) -> str | None:
    """The user_id, or None. Always hashes once, so a wrong email takes as long as a wrong password."""
    try:
        email = normalise_email(email)
    except ValueError:
        return None
    row = await pool.fetchrow("SELECT user_id, password_hash FROM users WHERE email = $1", email)
    ok = check_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if not (row and ok):
        return None
    await pool.execute("UPDATE users SET last_login_at = now() WHERE user_id = $1", row["user_id"])
    return row["user_id"]


_DUMMY_HASH = hash_password(secrets.token_urlsafe(12))


async def adopt_guest_data(pool, guest_session_id: str, user_id: str) -> int:
    """Signing in mid-visit keeps what the guest uploaded: it moves to the account. Returns clips moved."""
    async with pool.acquire() as conn, conn.transaction():
        moved = await conn.fetchval(
            "WITH m AS (UPDATE clips SET owner_user_id = $2 WHERE owner_session_id = $1 AND owner_user_id IS NULL "
            "RETURNING 1) SELECT count(*) FROM m", guest_session_id, user_id)
        await conn.execute("UPDATE equipment SET owner_user_id = $2 WHERE owner_session_id = $1 AND owner_user_id IS NULL",
                           guest_session_id, user_id)
        await conn.execute("UPDATE chat_messages SET visitor = $2 WHERE visitor = $1", guest_session_id, user_id)
        await conn.execute("UPDATE sessions SET ended_at = now(), end_reason = 'signed_in', purged_at = now() "
                           "WHERE session_id = $1 AND ended_at IS NULL", guest_session_id)
    return moved
