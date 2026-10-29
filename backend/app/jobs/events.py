"""Per-clip event stream in the clip_events table. The SSE endpoint reads it by event_id, so it works
across processes, survives reconnects (Last-Event-ID) and replays identically for the demo."""

import asyncpg

EVENT_TYPES = ("job_status", "displays_detected", "frame_processed", "reading", "read_failed", "physics_check", "relook_attempt",
               "anomaly", "diagnosis", "clip_done", "decision", "message")


async def emit(conn: asyncpg.Pool | asyncpg.Connection, clip_id: str, type_: str, payload: dict) -> int:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    return await conn.fetchval(
        "INSERT INTO clip_events (clip_id, type, payload) VALUES ($1, $2, $3) RETURNING event_id",
        clip_id, type_, payload)


async def fetch_after(pool: asyncpg.Pool, clip_id: str, after: int, limit: int = 200) -> list[dict]:
    rows = await pool.fetch(
        """SELECT event_id, ts, type, payload FROM clip_events
           WHERE clip_id = $1 AND event_id > $2 ORDER BY event_id LIMIT $3""", clip_id, after, limit)
    return [dict(r) for r in rows]
