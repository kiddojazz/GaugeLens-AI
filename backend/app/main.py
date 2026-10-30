"""GaugeLens API.

    GET /api/v1/health            database + model configuration (?deep=true also pings the model)
    + sessions and accounts (routers/auth.py), clip, readings, equipment and job routes (routers/clips.py), ...

On startup it opens the database pool, starts the guest-data sweeper (purge.py) and, when WORKER_IN_PROCESS=true,
the job worker.
Run locally:  uvicorn backend.app.main:app --reload --port 8000
"""

import asyncio
import contextlib
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from .access import optional_principal
from .config import PROJECT_ROOT, SHADOWED_ENV, Settings, get_settings, missing_settings
from .evidence.store import make_evidence_store
from .jobs.queue import PostgresJobQueue
from .llm import ChatClient, ModelUnavailable
from .limits import RateLimiter
from .purge import run_sweeper
from .routers import agent, auth, clips, diagnoses

log = logging.getLogger("gaugelens.api")


_SESSION_IN_URL = re.compile(r"([?&]s=)[^&\s]+")


class HideSessionTokens(logging.Filter):
    """Guest tokens travel in image and event URLs (?s=); access log lines keep the path without them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_SESSION_IN_URL.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


class SecurityHeaders:
    """Evidence holds bytes people uploaded: browsers must not guess its type. Nothing here belongs in someone's frame."""

    HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin")]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self.HEADERS]}
            await send(message)

        await self.app(scope, receive, with_headers)


class UploadSizeLimit:
    """Refuses an oversized upload from its Content-Length, before it is received. The route also counts the bytes
    it reads, for clients that don't declare a length. The margin covers the multipart wrapping."""

    def __init__(self, app, max_mb: int):
        self.app, self.max_mb = app, max_mb

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/api/v1/clips":
            declared = dict(scope["headers"]).get(b"content-length", b"0")
            if declared.isdigit() and int(declared) > (self.max_mb + 1) * 1024 * 1024:
                detail = f"upload larger than {self.max_mb} MB; record at 1080p and under 2 minutes"
                response = JSONResponse({"detail": detail}, status_code=413)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


class CompressText:
    """Gzip for the UI and JSON. Not for evidence (JPEG, PNG and video are already compressed) or live events
    (a compressing proxy holds back the stream)."""

    def __init__(self, app):
        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        plain = scope["type"] != "http" or path.endswith(("/evidence", "/events"))
        await (self.app if plain else self.gzip)(scope, receive, send)


def create_app(settings: Settings | None = None, client: ChatClient | None = None, pool=None,
               start_worker: bool | None = None) -> FastAPI:
    """Builds the app. `settings`, `client` and `pool` are injectable for tests."""
    settings = settings or get_settings()
    run_worker = settings.worker_in_process if start_worker is None else start_worker
    run_sweeper_task = start_worker is not False            # tests that drive the worker themselves sweep themselves

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        if SHADOWED_ENV:
            log.warning("these shell variables override different values in .env: %s. If that's unintended, "
                        "open a fresh terminal (or Remove-Item Env:<name>) and restart.", ", ".join(SHADOWED_ENV))
        state.settings, state.evidence = settings, make_evidence_store(settings)
        state.client = client or ChatClient(settings)
        state.pool, own_pool, worker_task, sweeper, stop = pool, False, None, None, asyncio.Event()
        if state.pool is None and settings.database_url:
            from .db.pool import create_pool
            try:
                state.pool, own_pool = await create_pool(settings.database_url), True
            except Exception as exc:  # noqa: BLE001 - the API still starts and /health reports it
                log.error("database unavailable: %s", exc)
        state.queue = PostgresJobQueue(state.pool) if state.pool is not None else None
        if state.pool is not None and run_worker:
            from .jobs.worker import Worker
            worker_task = asyncio.create_task(Worker(state.pool, settings, client=state.client,
                                                     evidence=state.evidence).run(stop))
        if state.pool is not None and run_sweeper_task:
            sweeper = asyncio.create_task(run_sweeper(state.pool, state.evidence, settings, stop))
        yield
        stop.set()
        for task in (worker_task, sweeper):
            if task is not None:
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=10)
        if own_pool:
            await state.pool.close()

    app = FastAPI(title="GaugeLens API", version="0.3.0", lifespan=lifespan)
    app.state.limiter = RateLimiter()
    app.state.upload_gate = asyncio.Semaphore(2)            # uploads are held in memory: at most this many at once
    app.add_middleware(UploadSizeLimit, max_mb=settings.max_upload_mb)
    app.add_middleware(CompressText)
    app.add_middleware(SecurityHeaders)
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HideSessionTokens) for f in access_log.filters):
        access_log.addFilter(HideSessionTokens())
    app.include_router(auth.router)
    app.include_router(clips.router)
    app.include_router(diagnoses.router)
    app.include_router(agent.router)

    async def _admin(request: Request) -> bool:
        principal = await optional_principal(request) if getattr(app.state, "pool", None) is not None else None
        return principal is not None and principal.is_admin

    @app.get("/api/v1/health")
    async def health(request: Request, deep: bool = False):
        db = "not configured"
        pool_ = getattr(app.state, "pool", None)
        if pool_ is not None:
            try:
                await pool_.fetchval("SELECT 1")
                db = "ok"
            except Exception as exc:  # noqa: BLE001 - health reports, never raises
                db = f"error: {type(exc).__name__}"
        elif settings.database_url:
            db = "unavailable"

        missing = missing_settings(settings)
        model = {"provider": "meta", "model": settings.read_model,
                 "cache_only": settings.cache_only, "configured": not missing}
        if missing:
            model["missing"] = missing
        elif deep and not settings.cache_only and await _admin(request):     # a ping spends model credits
            try:
                _, latency = await app.state.client.ping()
                model["ping_ms"] = latency
            except ModelUnavailable as exc:
                model["ping_error"] = str(exc)[:200]
        status = "ok" if db == "ok" and not missing and "ping_error" not in model else "degraded"
        return {"status": status, "db": db, "model": model, "worker": "in-process" if run_worker else "external"}

    # The built web UI (web/dist), served from the same container (one app, one URL, no CORS).
    # Registered last so /api/v1/* routes win; the UI uses hash routes, so deep links never 404.
    web_dist = PROJECT_ROOT / "web" / "dist"
    if web_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        class WebFiles(StaticFiles):
            """Built assets have a content hash in their name, so they can be cached for good; index.html never."""

            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                                     if path.replace("\\", "/").startswith("assets/") else "no-cache")
                return response

        app.mount("/", WebFiles(directory=web_dist, html=True), name="web")
    return app


app = create_app()
