"""Job queue against a real Postgres (gaugelens_test)."""

import asyncio

from backend.app.db.pool import create_pool
from backend.app.jobs.queue import PostgresJobQueue


def run(db_url, scenario):
    async def main():
        pool = await create_pool(db_url, min_size=2, max_size=4)
        try:
            return await scenario(pool, PostgresJobQueue(pool))
        finally:
            await pool.close()
    return asyncio.run(main())


def test_enqueue_claim_complete(clean_db):
    async def scenario(pool, q):
        job_id = await q.enqueue("process_clip", {"clip_id": "c1"})
        job = await q.claim("w1")
        assert job.job_id == job_id and job.payload == {"clip_id": "c1"} and job.attempts == 1
        assert await q.claim("w2") is None                      # nothing else is ready
        await q.complete(job_id)
        assert (await q.get(job_id))["status"] == "done"
    run(clean_db, scenario)


def test_concurrent_claims_never_return_the_same_job(clean_db):
    async def scenario(pool, q):
        ids = {await q.enqueue("process_clip", {"n": n}) for n in range(5)}
        claimed = await asyncio.gather(*(q.claim(f"w{n}") for n in range(8)))
        got = [j.job_id for j in claimed if j is not None]
        assert len(got) == 5 and set(got) == ids
    run(clean_db, scenario)


def test_dedupe_key_returns_the_existing_job(clean_db):
    async def scenario(pool, q):
        first = await q.enqueue("diagnose", {"anomaly_id": "a1"}, dedupe_key="diagnose:a1")
        second = await q.enqueue("diagnose", {"anomaly_id": "a1"}, dedupe_key="diagnose:a1")
        assert first == second and await pool.fetchval("SELECT count(*) FROM jobs") == 1
    run(clean_db, scenario)


def test_failures_back_off_then_go_dead_and_can_be_retried(clean_db):
    async def scenario(pool, q):
        job_id = await q.enqueue("process_clip", {}, max_attempts=2)
        await q.claim("w")
        assert await q.fail(job_id, "boom 1") == "queued"
        assert await q.claim("w") is None                       # still in backoff
        await pool.execute("UPDATE jobs SET run_after = now() WHERE job_id = $1", job_id)
        assert (await q.claim("w")).attempts == 2
        assert await q.fail(job_id, "boom 2") == "dead"
        assert (await q.get(job_id))["last_error"] == "boom 2"
        assert await q.retry(job_id) and (await q.claim("w")).attempts == 1
    run(clean_db, scenario)


def test_expired_lease_is_reclaimed(clean_db):
    async def scenario(pool, q):
        job_id = await q.enqueue("process_clip", {})
        await q.claim("crashed-worker")
        await pool.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE job_id = $1", job_id)
        job = await q.claim("healthy-worker")
        assert job.job_id == job_id and job.attempts == 2
    run(clean_db, scenario)
