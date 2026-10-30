"""Creating a clip: store the source video (or photos) as evidence, insert the clip row, enqueue process_clip.

Browser recordings (the Scan page: MediaRecorder) are often WebM; when ffmpeg is installed (the Docker image has
it) they are converted to H.264 MP4 first, the format every later step handles best (OpenCV, SAM 3.1 tracking).
"""

import asyncio
import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from ..evidence.store import LocalEvidenceStore
from .ingest import normalise_photo, photo_times
from ..ids import new_id
from ..jobs.events import emit
from ..jobs.queue import PostgresJobQueue

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".3gp", ".webm", ".mkv", ".avi"}
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_PHOTOS = 10
TRANSCODE = {".webm", ".mkv", ".avi"}
log = logging.getLogger(__name__)


def to_mp4(video: bytes, suffix: str) -> bytes | None:
    """H.264 MP4 of a browser recording, or None when ffmpeg isn't available or fails (the original is kept)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / f"in{suffix}", Path(tmp) / "out.mp4"
        src.write_bytes(video)
        result = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-an", "-c:v", "libx264",
                                 "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                                 str(dst)], capture_output=True, timeout=300)
        if result.returncode != 0 or not dst.is_file():
            log.warning("ffmpeg could not convert the %s recording: %s", suffix, result.stderr[-300:])
            return None
        return dst.read_bytes()


class ClipError(ValueError):
    pass


def suffix_of(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_photo(filename: str) -> bool:
    return suffix_of(filename) in PHOTO_SUFFIXES or suffix_of(filename) in (".heic", ".heif")


async def create_clip(pool: asyncpg.Pool, evidence: LocalEvidenceStore, queue: PostgresJobQueue, *,
                      video: bytes, filename: str, equipment_id: str | None, recorded_at: datetime | None = None,
                      channel: str = "web", is_demo_replay: bool = False,
                      owner: tuple[str | None, str | None] = (None, None)) -> tuple[str, str]:
    """Returns (clip_id, job_id). equipment_id None: identified from the video when processed.
    owner = (user_id, session_id) of the uploader (access.py); (None, None) = admins only (seed)."""
    suffix = suffix_of(filename)
    if suffix not in VIDEO_SUFFIXES:
        raise ClipError(f"unsupported video type '{suffix or filename}'; use one of {sorted(VIDEO_SUFFIXES)}")
    await _check_equipment(pool, equipment_id)

    if suffix in TRANSCODE:
        converted = await asyncio.to_thread(to_mp4, video, suffix)
        if converted:
            video, suffix = converted, ".mp4"
    clip_id = new_id("clip_")
    source_uri = await asyncio.to_thread(evidence.write, f"{evidence.clip_dir(clip_id)}/source{suffix}", video)
    await pool.execute(
        """INSERT INTO clips (clip_id, equipment_id, recorded_at, source_uri, source_channel, is_demo_replay,
                             owner_user_id, owner_session_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        clip_id, equipment_id, recorded_at or datetime.now(timezone.utc), source_uri, channel, is_demo_replay, *owner)
    job_id = await queue.enqueue("process_clip", {"clip_id": clip_id}, dedupe_key=f"process_clip:{clip_id}")
    await emit(pool, clip_id, "job_status", {"status": "queued", "job_id": job_id})
    return clip_id, job_id


async def _check_equipment(pool, equipment_id: str | None) -> None:
    if equipment_id is not None and not await pool.fetchval("SELECT 1 FROM equipment WHERE equipment_id = $1", equipment_id):
        raise ClipError(f"unknown equipment '{equipment_id}'")


async def create_photo_clip(pool: asyncpg.Pool, evidence: LocalEvidenceStore, queue: PostgresJobQueue, *,
                            photos: list[tuple[str, bytes]], equipment_id: str | None, channel: str = "web",
                            is_demo_replay: bool = False, owner: tuple[str | None, str | None] = (None, None)
                            ) -> tuple[str, str]:
    """A clip made of photos [(filename, bytes)]: each becomes one frame. Several photos of a screen that cycles
    pages read like a short video; one photo still gives every value and the checks that need only one moment."""
    if not photos:
        raise ClipError("no photos")
    if len(photos) > MAX_PHOTOS:
        raise ClipError(f"send at most {MAX_PHOTOS} photos at a time")
    for name, _ in photos:
        if suffix_of(name) in (".heic", ".heif"):
            raise ClipError("HEIC photos can't be read yet; on an iPhone, set Camera > Formats to 'Most Compatible' "
                            "or share the photo as JPEG")
        if suffix_of(name) not in PHOTO_SUFFIXES:
            raise ClipError(f"unsupported photo type '{suffix_of(name) or name}'; use JPEG, PNG or WebP")
    await _check_equipment(pool, equipment_id)
    try:
        normalised = [await asyncio.to_thread(normalise_photo, data) for _, data in photos]
    except ValueError as exc:
        raise ClipError(str(exc)) from exc
    offsets, timed = photo_times([taken for _, taken in normalised])
    clip_id = new_id("clip_")
    stored = []
    for n, ((jpeg, _), t) in enumerate(zip(normalised, offsets), start=1):
        uri = await asyncio.to_thread(evidence.write, f"{evidence.clip_dir(clip_id)}/photos/p{n:02d}.jpg", jpeg)
        stored.append({"uri": uri, "t": t})
    media = {"kind": "photos", "timed": timed, "photos": stored}
    await pool.execute(
        """INSERT INTO clips (clip_id, equipment_id, recorded_at, source_uri, source_channel, is_demo_replay,
                             owner_user_id, owner_session_id, media)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
        clip_id, equipment_id, datetime.now(timezone.utc), stored[0]["uri"], channel, is_demo_replay, *owner, media)
    job_id = await queue.enqueue("process_clip", {"clip_id": clip_id}, dedupe_key=f"process_clip:{clip_id}")
    await emit(pool, clip_id, "job_status", {"status": "queued", "job_id": job_id})
    return clip_id, job_id
