"""PostgresJobQueue: jobs are claimed with FOR UPDATE SKIP LOCKED, so any number of workers is safe.

    queued -> running (lease) -> done
                         |-> failed attempt -> queued again after backoff (10 s x 2^attempts)
                         |-> attempts exhausted -> dead (the UI offers a retry)
A worker that dies keeps its lease until `locked_until`; after that the job is claimable again.
"""

from dataclasses import dataclass

import asyncpg

from ..ids import new_id

KINDS = ("process_clip", "diagnose", "run_eval", "demo_replay")

_CLAIM = """
UPDATE jobs
SET status = 'running', attempts = attempts + 1, locked_by = $1,
    locked_until = now() + make_interval(secs => $2), updated_at = now()
WHERE job_id = (
  SELECT job_id FROM jobs
  WHERE (status = 'queued' AND run_after <= now())
     OR (status = 'running' AND locked_until < now())
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1)
RETURNING job_id, kind, payload, attempts, max_attempts
"""


@dataclass
class Job:
    job_id: str
    kind: str
    payload: dict
    attempts: int
    max_attempts: int


class PostgresJobQueue:
    def __init__(self, pool: asyncpg.Pool, lease_s: float = 120.0):
        self.pool = pool
        self.lease_s = lease_s

    async def enqueue(self, kind: str, payload: dict, dedupe_key: str | None = None, max_attempts: int = 3) -> str:
        """Returns the new job id, or the existing job's id when `dedupe_key` was already used."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        async with self.pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jobs (job_id, kind, payload, dedupe_key, max_attempts)
                   VALUES ($1, $2, $3, $4, $5) ON CONFLICT (dedupe_key) DO NOTHING RETURNING job_id""",
                new_id("job_"), kind, payload, dedupe_key, max_attempts)
            if job_id is None:
                job_id = await conn.fetchval("SELECT job_id FROM jobs WHERE dedupe_key = $1", dedupe_key)
        return job_id

    async def claim(self, worker_id: str) -> Job | None:
        row = await self.pool.fetchrow(_CLAIM, worker_id, self.lease_s)
        return Job(**dict(row)) if row else None

    async def heartbeat(self, job_id: str, progress: float | None = None) -> None:
        await self.pool.execute(
            """UPDATE jobs SET locked_until = now() + make_interval(secs => $2),
                   progress = COALESCE($3, progress), updated_at = now()
               WHERE job_id = $1 AND status = 'running'""", job_id, self.lease_s, progress)

    async def complete(self, job_id: str) -> None:
        await self.pool.execute(
            """UPDATE jobs SET status = 'done', progress = 1, locked_by = NULL, locked_until = NULL,
                   last_error = NULL, updated_at = now() WHERE job_id = $1""", job_id)

    async def fail(self, job_id: str, error: str) -> str:
        """Re-queues with exponential backoff, or marks the job dead. Returns the new status."""
        return await self.pool.fetchval(
            """UPDATE jobs SET
                   status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                   run_after = now() + make_interval(secs => 10 * power(2, attempts)),
                   last_error = $2, locked_by = NULL, locked_until = NULL, updated_at = now()
               WHERE job_id = $1 RETURNING status""", job_id, error[:2000])

    async def get(self, job_id: str) -> dict | None:
        row = await self.pool.fetchrow(
            """SELECT job_id, kind, payload, status, attempts, max_attempts, progress, last_error,
                      created_at, updated_at FROM jobs WHERE job_id = $1""", job_id)
        return dict(row) if row else None

    async def retry(self, job_id: str) -> bool:
        """Puts a dead or failed job back in the queue with a fresh attempt budget."""
        status = await self.pool.fetchval(
            """UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), updated_at = now()
               WHERE job_id = $1 AND status IN ('dead', 'failed') RETURNING status""", job_id)
        return status is not None
