"""Creating a clip: store the source video as evidence, insert the clip row, enqueue process_clip."""

from datetime import datetime, timezone

import asyncpg

from ..evidence.store import LocalEvidenceStore
from ..ids import new_id
from ..jobs.events import emit
from ..jobs.queue import PostgresJobQueue

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".3gp", ".webm", ".mkv", ".avi"}


class ClipError(ValueError):
    pass


async def create_clip(pool: asyncpg.Pool, evidence: LocalEvidenceStore, queue: PostgresJobQueue, *,
                      video: bytes, filename: str, equipment_id: str, recorded_at: datetime | None = None,
                      channel: str = "web", is_demo_replay: bool = False) -> tuple[str, str]:
    """Returns (clip_id, job_id)."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in VIDEO_SUFFIXES:
        raise ClipError(f"unsupported video type '{suffix or filename}'; use one of {sorted(VIDEO_SUFFIXES)}")
    if not await pool.fetchval("SELECT 1 FROM equipment WHERE equipment_id = $1", equipment_id):
        raise ClipError(f"unknown equipment '{equipment_id}'")

    clip_id = new_id("clip_")
    source_uri = evidence.write(f"{evidence.clip_dir(clip_id)}/source{suffix}", video)
    await pool.execute(
        """INSERT INTO clips (clip_id, equipment_id, recorded_at, source_uri, source_channel, is_demo_replay)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        clip_id, equipment_id, recorded_at or datetime.now(timezone.utc), source_uri, channel, is_demo_replay)
    job_id = await queue.enqueue("process_clip", {"clip_id": clip_id}, dedupe_key=f"process_clip:{clip_id}")
    await emit(pool, clip_id, "job_status", {"status": "queued", "job_id": job_id})
    return clip_id, job_id
