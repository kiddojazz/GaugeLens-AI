"""Job worker: claims jobs from the Postgres queue and runs their handler.

v1 runs inside the API process (WORKER_IN_PROCESS=true). It can also run on its own, from the same
image, as a second container:

    python -m backend.app.jobs.worker
"""

import asyncio
import logging
import os
import socket
import sys
from dataclasses import dataclass

import asyncpg

from ..config import Settings, get_settings, missing_settings
from ..evidence.store import LocalEvidenceStore, make_evidence_store
from ..ids import new_id
from ..llm import ChatClient
from .events import emit
from .queue import Job, PostgresJobQueue

log = logging.getLogger(__name__)


@dataclass
class JobContext:
    pool: asyncpg.Pool
    settings: Settings
    client: ChatClient
    evidence: LocalEvidenceStore
    queue: PostgresJobQueue
    job: Job
    sam: object = None                   # SamClient when SEG_PROVIDER=meta

    async def progress(self, fraction: float) -> None:
        await self.queue.heartbeat(self.job.job_id, round(min(1.0, max(0.0, fraction)), 3))


def default_handlers() -> dict:
    from ..agent.diagnose import diagnose_job
    from ..agent.evals import eval_job
    from ..channels.conversation import ingest_job, notify_job
    from ..pipeline.process_clip import process_clip
    return {"process_clip": process_clip, "diagnose": diagnose_job, "run_eval": eval_job,
            "whatsapp_ingest": ingest_job, "notify": notify_job}


class Worker:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, client: ChatClient | None = None,
                 evidence: LocalEvidenceStore | None = None, handlers: dict | None = None,
                 worker_id: str | None = None, poll_s: float = 1.0, sam=None):
        self.pool, self.settings = pool, settings
        self.client = client or ChatClient(settings)
        if sam is None and settings.seg_provider == "meta":
            from ..llm.sam import SamClient
            sam = SamClient(settings)
        self.sam = sam
        self.evidence = evidence or make_evidence_store(settings)
        self.queue = PostgresJobQueue(pool)
        self.handlers = handlers if handlers is not None else default_handlers()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{new_id()[-6:]}"
        self.poll_s = poll_s

    async def run_once(self) -> Job | None:
        """Claims and runs at most one job. Returns the job, or None when the queue was empty."""
        job = await self.queue.claim(self.worker_id)
        if job is None:
            return None
        ctx = JobContext(self.pool, self.settings, self.client, self.evidence, self.queue, job, sam=self.sam)
        heartbeat = asyncio.create_task(self._heartbeat(job))
        clip_id = job.payload.get("clip_id")
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                raise RuntimeError(f"no handler for job kind '{job.kind}'")
            await handler(job, ctx)
        except Exception as exc:  # noqa: BLE001 - any handler failure is recorded and retried, never lost
            log.exception("job %s (%s) failed", job.job_id, job.kind)
            status = await self.queue.fail(job.job_id, f"{type(exc).__name__}: {exc}")
            if clip_id:
                if status == "dead" and job.kind == "process_clip":
                    await self.pool.execute("UPDATE clips SET status = 'failed' WHERE clip_id = $1", clip_id)
                    from ..channels.conversation import notify_if_channel
                    await notify_if_channel(self.pool, self.queue, clip_id, "clip_failed")
                await emit(self.pool, clip_id, "job_status", {"status": status, "job_id": job.job_id, "kind": job.kind,
                                                               "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        else:
            await self.queue.complete(job.job_id)
            if clip_id:
                await emit(self.pool, clip_id, "job_status", {"status": "done", "job_id": job.job_id, "kind": job.kind})
        finally:
            heartbeat.cancel()
        return job

    async def _heartbeat(self, job: Job) -> None:
        while True:
            await asyncio.sleep(self.queue.lease_s / 4)
            await self.queue.heartbeat(job.job_id)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("worker %s started", self.worker_id)
        while not stop.is_set():
            try:
                job = await self.run_once()
            except (asyncpg.PostgresError, OSError) as exc:     # database blip: back off, keep the worker alive
                log.warning("worker: database error %s; retrying in 5 s", exc)
                job = None
                await asyncio.sleep(5)
            if job is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
                except asyncio.TimeoutError:
                    pass
        log.info("worker %s stopped", self.worker_id)


async def _main() -> int:
    from ..db.pool import create_pool

    settings = get_settings()
    missing = missing_settings(settings) + ([] if settings.database_url else ["DATABASE_URL"])
    if missing:
        print("Missing settings: " + ", ".join(missing))
        return 1
    pool = await create_pool(settings.database_url)
    stop = asyncio.Event()
    try:
        await Worker(pool, settings).run(stop)
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)
