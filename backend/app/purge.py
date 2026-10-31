"""Deleting guest data. A guest session ends when its page is refreshed or closed (the page sends a beacon) or after
GUEST_IDLE_MINUTES without a request; then its clips and everything derived from them are deleted: database rows,
the source video, frames and crops (blob and local cache), and equipment it registered.

Accounts are never purged. A session whose clip is still being processed waits for the job, then is purged on the
next sweep (run every SWEEP_SECONDS by the API process).
"""

import asyncio
import logging

log = logging.getLogger("gaugelens.purge")
SWEEP_SECONDS = 60

# Child rows first. $1 = the clip ids; each statement is scoped to those clips only.
_DELETE = [
    "DELETE FROM agent_runs WHERE clip_id = ANY($1) OR anomaly_id IN (SELECT anomaly_id FROM anomalies WHERE clip_id = ANY($1))",
    "DELETE FROM diagnoses WHERE anomaly_id IN (SELECT anomaly_id FROM anomalies WHERE clip_id = ANY($1))",
    "DELETE FROM anomalies WHERE clip_id = ANY($1)",
    "DELETE FROM chat_messages WHERE clip_id = ANY($1)",
    "DELETE FROM physics_checks WHERE clip_id = ANY($1)",
    "DELETE FROM readings WHERE frame_id IN (SELECT frame_id FROM frames WHERE clip_id = ANY($1))",
    "DELETE FROM frames WHERE clip_id = ANY($1)",
    "DELETE FROM clip_rois WHERE clip_id = ANY($1)",
    "DELETE FROM clip_annotations WHERE clip_id = ANY($1)",
    "DELETE FROM clip_events WHERE clip_id = ANY($1)",
    "DELETE FROM jobs WHERE payload->>'clip_id' = ANY($1) OR dedupe_key = ANY(SELECT 'process_clip:' || unnest($1::text[]))",
    "DELETE FROM clips WHERE clip_id = ANY($1)",
]


async def purge_session(pool, evidence, session_id: str) -> int | None:
    """Deletes an ended guest session's data. Returns clips deleted, or None if a job is still running (retry later)."""
    clip_ids = [r["clip_id"] for r in await pool.fetch(
        "SELECT clip_id FROM clips WHERE owner_session_id = $1 AND owner_user_id IS NULL", session_id)]
    if clip_ids and await pool.fetchval(
            "SELECT 1 FROM jobs WHERE status = 'running' AND locked_until > now() AND payload->>'clip_id' = ANY($1)",
            clip_ids):
        return None
    async with pool.acquire() as conn, conn.transaction():
        # their conversations about example clips, which aren't theirs and stay
        await conn.execute("DELETE FROM agent_runs WHERE run_id IN (SELECT run_id FROM chat_messages WHERE visitor = $1)",
                           session_id)
        await conn.execute("DELETE FROM chat_messages WHERE visitor = $1", session_id)
        for statement in _DELETE:
            await conn.execute(statement, clip_ids)
        equipment = [r["equipment_id"] for r in await conn.fetch(
            """SELECT equipment_id FROM equipment e WHERE owner_session_id = $1 AND owner_user_id IS NULL
                 AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.equipment_id = e.equipment_id)""", session_id)]
        if equipment:
            await conn.execute("DELETE FROM physics_checks WHERE equipment_id = ANY($1)", equipment)
            await conn.execute("DELETE FROM anomalies WHERE equipment_id = ANY($1)", equipment)
            await conn.execute("DELETE FROM clip_rois WHERE display_id IN (SELECT display_id FROM displays "
                               "WHERE equipment_id = ANY($1))", equipment)
            await conn.execute("DELETE FROM instruments WHERE equipment_id = ANY($1)", equipment)
            await conn.execute("DELETE FROM displays WHERE equipment_id = ANY($1)", equipment)
            await conn.execute("DELETE FROM equipment WHERE equipment_id = ANY($1)", equipment)
        await conn.execute("UPDATE sessions SET purged_at = now() WHERE session_id = $1", session_id)
    files = 0
    for clip_id in clip_ids:                     # after the rows: a failure here leaves files, never orphaned rows
        try:
            files += await asyncio.to_thread(evidence.delete_clip, clip_id)
        except Exception as exc:  # noqa: BLE001 - logged; the sweep doesn't retry files, so say which clip
            log.error("could not delete the files of clip %s: %s", clip_id, exc)
    if clip_ids:
        log.info("guest session %s purged: %d clips, %d files", session_id, len(clip_ids), files)
    return len(clip_ids)


async def sweep(pool, evidence, settings) -> int:
    """Ends idle guest sessions and expired accounts' sessions, then purges ended guest sessions. Returns clips deleted."""
    await pool.execute(
        "UPDATE sessions SET ended_at = now(), end_reason = 'idle' WHERE kind = 'guest' AND ended_at IS NULL "
        "AND last_seen_at < now() - make_interval(mins => $1)", settings.guest_idle_minutes)
    await pool.execute("UPDATE sessions SET ended_at = now(), end_reason = 'expired' WHERE kind = 'user' "
                       "AND ended_at IS NULL AND expires_at < now()")
    deleted = 0
    for row in await pool.fetch("SELECT session_id FROM sessions WHERE kind = 'guest' AND ended_at IS NOT NULL "
                                "AND purged_at IS NULL ORDER BY ended_at LIMIT 50"):
        deleted += await purge_session(pool, evidence, row["session_id"]) or 0
    return deleted


async def run_sweeper(pool, evidence, settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await sweep(pool, evidence, settings)
        except Exception as exc:  # noqa: BLE001 - keep sweeping; the next run picks up where this one failed
            log.error("guest data sweep failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
