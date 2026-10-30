"""Sessions and accounts.

    POST /api/v1/auth/guest           start a guest session -> {token, ...}; the page keeps the token in memory only
    POST /api/v1/auth/guest/end       body = the guest token (sendBeacon on page exit); its data is deleted
    GET  /api/v1/auth/me              who the caller is (account cookie or guest token)
    POST /api/v1/auth/register        {email, password} -> account + cookie; the caller's guest clips move to it
    POST /api/v1/auth/login           {email, password} -> cookie; the caller's guest clips move to the account
    POST /api/v1/auth/logout
    GET  /api/v1/admin/people         admins: accounts and live guest sessions, with what each holds
"""

import asyncio
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import access
from ..access import Principal, admin_principal, current_principal
from ..limits import client_ip, enforce
from ..purge import purge_session
from .clips import _jsonable, _state

router = APIRouter(prefix="/api/v1")

LOGIN_WINDOW_S, LOGIN_MAX_FAILURES = 15 * 60, 8
_failures: dict[str, deque] = defaultdict(deque)       # per client address and per email; in memory (one replica)
_purges: set[asyncio.Task] = set()                     # keeps a reference so a running purge isn't collected


def _limited(*keys: str) -> bool:
    now = time.monotonic()
    for key in keys:
        q = _failures[key]
        while q and now - q[0] > LOGIN_WINDOW_S:
            q.popleft()
        if len(q) >= LOGIN_MAX_FAILURES:
            return True
    return False


def _fail(*keys: str) -> None:
    for key in keys:
        _failures[key].append(time.monotonic())


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


async def _me(state, principal: Principal) -> dict:
    out = {"kind": principal.kind, "email": principal.email, "session_id": principal.session_id,
           "idle_minutes": state.settings.guest_idle_minutes}
    if principal.kind == "guest":
        used = await state.pool.fetchval("SELECT count(*) FROM clips WHERE owner_session_id = $1 "
                                         "AND owner_user_id IS NULL", principal.session_id)
        out["clips_left"] = max(0, state.settings.guest_max_clips - used)
    return out


@router.post("/auth/guest")
async def start_guest(request: Request):
    state = _state(request)
    enforce(request, f"ip:{client_ip(request)}:sessions", state.settings.guest_sessions_per_hour, "new sessions")
    session_id, token = await access.start_session(state.pool, "guest")
    return {"token": token, "kind": "guest", "session_id": session_id, "email": None,
            "idle_minutes": state.settings.guest_idle_minutes, "clips_left": state.settings.guest_max_clips}


@router.post("/auth/guest/end", status_code=204)
async def end_guest(request: Request):
    """Called with navigator.sendBeacon when the page is closed or refreshed (no headers, so the token is the body)."""
    state = _state(request)
    token = (await request.body()).decode("utf-8", "ignore").strip()
    session_id = await access.end_session(state.pool, token, "left") if token else None
    if session_id:
        task = asyncio.create_task(purge_session(state.pool, state.evidence, session_id))  # the sweep retries if busy
        _purges.add(task)
        task.add_done_callback(_purges.discard)


@router.get("/auth/me")
async def me(request: Request, principal: Principal = Depends(current_principal)):
    return await _me(_state(request), principal)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


async def _sign_in(request: Request, response: Response, user_id: str) -> dict:
    state = _state(request)
    guest = await access.guest_token_principal(request)
    moved = await access.adopt_guest_data(state.pool, guest.session_id, user_id) if guest else 0
    _, token = await access.start_session(state.pool, "user", user_id, days=state.settings.user_session_days)
    response.set_cookie(access.COOKIE, token, max_age=state.settings.user_session_days * 86400, httponly=True,
                        secure=_secure(request), samesite="lax", path="/")
    principal = await access.principal_for(state.pool, state.settings, token, via_cookie=True)
    return {**await _me(state, principal), "clips_moved": moved}


@router.post("/auth/register")
async def register(request: Request, response: Response, body: Credentials):
    state = _state(request)
    if _limited(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many attempts; try again in 15 minutes")
    try:
        user_id = await access.create_user(state.pool, body.email, body.password)
    except LookupError as exc:
        _fail(client_ip(request))                         # slows down probing for registered emails
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _sign_in(request, response, user_id)


@router.post("/auth/login")
async def login(request: Request, response: Response, body: Credentials):
    state = _state(request)
    keys = (client_ip(request), f"email:{body.email.strip().lower()}")
    if _limited(*keys):
        raise HTTPException(status_code=429, detail="too many attempts; try again in 15 minutes")
    user_id = await access.authenticate(state.pool, body.email, body.password)
    if user_id is None:
        _fail(*keys)
        raise HTTPException(status_code=401, detail="wrong email or password")
    return await _sign_in(request, response, user_id)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    state = _state(request)
    token = request.cookies.get(access.COOKIE)
    if token:
        await access.end_session(state.pool, token, "logout")
    response.delete_cookie(access.COOKIE, path="/")


@router.get("/admin/people")
async def people(request: Request, principal: Principal = Depends(admin_principal)):
    state = _state(request)
    users = await state.pool.fetch(
        """SELECT u.user_id, u.email, u.role, u.created_at, u.last_login_at,
                  (SELECT count(*) FROM clips c WHERE c.owner_user_id = u.user_id) AS clips,
                  (SELECT count(*) FROM chat_messages m JOIN clips c USING (clip_id)
                    WHERE c.owner_user_id = u.user_id AND m.role = 'user') AS questions,
                  (SELECT max(last_seen_at) FROM sessions s WHERE s.user_id = u.user_id) AS last_seen_at
           FROM users u ORDER BY u.created_at DESC""")
    guests = await state.pool.fetch(
        """SELECT s.session_id, s.created_at, s.last_seen_at,
                  (SELECT count(*) FROM clips c WHERE c.owner_session_id = s.session_id AND c.owner_user_id IS NULL) AS clips,
                  (SELECT count(*) FROM chat_messages m JOIN clips c USING (clip_id)
                    WHERE c.owner_session_id = s.session_id AND c.owner_user_id IS NULL AND m.role = 'user') AS questions
           FROM sessions s WHERE s.kind = 'guest' AND s.ended_at IS NULL
             AND s.last_seen_at > now() - make_interval(mins => $1)
           ORDER BY s.last_seen_at DESC LIMIT 200""", state.settings.guest_idle_minutes)
    purged = await state.pool.fetchrow(
        """SELECT count(*) AS sessions, max(purged_at) AS last FROM sessions
           WHERE kind = 'guest' AND purged_at > now() - interval '7 days'""")
    unowned = await state.pool.fetch(
        """SELECT source_channel, count(*) AS clips FROM clips WHERE owner_user_id IS NULL AND owner_session_id IS NULL
           GROUP BY source_channel ORDER BY source_channel""")
    return {"users": [_jsonable(dict(r)) for r in users], "guests": [_jsonable(dict(r)) for r in guests],
            "purged_last_7_days": _jsonable(dict(purged)), "unowned": [dict(r) for r in unowned],
            "idle_minutes": state.settings.guest_idle_minutes}
