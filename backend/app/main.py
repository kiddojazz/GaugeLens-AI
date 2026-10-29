"""GaugeLens API.

    GET /api/v1/health            database + model configuration (?deep=true also pings the model)
    + clip, readings, equipment and job routes (routers/clips.py)

On startup it opens the database pool and, when WORKER_IN_PROCESS=true, starts the job worker.
Run locally:  uvicorn backend.app.main:app --reload --port 8000
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import PROJECT_ROOT, SHADOWED_ENV, Settings, get_settings, missing_settings
from .evidence.store import make_evidence_store
from .jobs.queue import PostgresJobQueue
from .llm import ChatClient, ModelUnavailable
from .routers import agent, clips, diagnoses, whatsapp

log = logging.getLogger("gaugelens.api")


def create_app(settings: Settings | None = None, client: ChatClient | None = None, pool=None,
               start_worker: bool | None = None) -> FastAPI:
    """Builds the app. `settings`, `client` and `pool` are injectable for tests."""
    settings = settings or get_settings()
    run_worker = settings.worker_in_process if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        if SHADOWED_ENV:
            log.warning("these shell variables override different values in .env: %s. If that's unintended, "
                        "open a fresh terminal (or Remove-Item Env:<name>) and restart.", ", ".join(SHADOWED_ENV))
        state.settings, state.evidence = settings, make_evidence_store(settings)
        state.client = client or ChatClient(settings)
        state.pool, own_pool, worker_task, stop = pool, False, None, asyncio.Event()
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
        yield
        stop.set()
        if worker_task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(worker_task, timeout=10)
        if own_pool:
            await state.pool.close()

    app = FastAPI(title="GaugeLens API", version="0.3.0", lifespan=lifespan)
    app.include_router(clips.router)
    app.include_router(diagnoses.router)
    app.include_router(agent.router)
    app.include_router(whatsapp.router)

    @app.get("/api/v1/health")
    async def health(deep: bool = False):
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
        elif deep and not settings.cache_only:
            try:
                _, latency = await (client or ChatClient(settings)).ping()
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
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app


app = create_app()
