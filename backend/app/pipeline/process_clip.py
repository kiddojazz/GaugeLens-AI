"""The process_clip job: video -> sampled frames -> display crops -> readings (with lineage) + events.

Display regions are resolved once per clip (SAM 3.1 or manual ROIs, see discovery.py). Per frame and
display: re-read only if the crop changed or REREAD_INTERVAL_S passed; otherwise the previous readings
are stored again with `reused_from`. Every reading keeps its frame, crop, model call, raw text and
display page. The physics stage then checks them; illegible fields start as 'needs_review'.

Idempotent: frame ids are deterministic and readings are unique per (ts, frame, instrument, attempt),
so a retried job resumes after the last frame that emitted frame_processed.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from ..ids import new_id
from ..jobs.events import emit
from ..llm import CacheMiss, ModelUnavailable, StructuredOutputError
from .calls import record_call
from .discovery import resolve_rois
from .identify import identify_clip
from .ingest import SampledFrame, changed, crop_signature, effective_fps, photo_frames, sample_frames
from .mapping import map_fields
from .physics_stage import PhysicsStage
from .relook import look_again
from .read_digital import prepare_image, read_display

log = logging.getLogger(__name__)

_DISPLAYS = """
SELECT d.display_id, d.equipment_id, d.kind, d.label, d.roi, d.roi_frame_size, d.context, d.sam_prompt,
       d.layout_rank
FROM displays d JOIN equipment e ON e.equipment_id = d.equipment_id
WHERE d.kind = 'digital' AND (e.equipment_id = $1 OR e.linked_equipment_id = $1)
ORDER BY d.display_id
"""
_INSTRUMENTS = """
SELECT instrument_id, display_id, unit, physics_role, match, resolution
FROM instruments WHERE display_id = ANY($1::text[])
"""
_INSERT_READING = """
INSERT INTO readings (ts, reading_id, instrument_id, frame_id, value, unit, raw_text, display_page, method,
                      confidence, status, attempt, reused_from, crop_uri, model_call_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'llm', $9, $10, 1, $11, $12, $13)
ON CONFLICT (ts, frame_id, instrument_id, attempt) DO NOTHING
RETURNING reading_id
"""


@dataclass
class DisplayState:
    signature: object = None           # thumbnail of the last crop that was actually read
    last_read_t: float = -1e9
    last: list = field(default_factory=list)   # readings from the last real read (dicts)


def confidence_for(fld, reading) -> float:
    """Reader-side confidence only; physics and two-frame agreement adjust it later.
    readers rarely mark their own errors illegible (docs/spike_results.md), so this is deliberately modest."""
    if not fld.legible or fld.value is None:
        return 0.0
    return 0.6 if reading.glare_or_occlusion else 0.8


def _reading_event(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


async def _read_display(ctx, clip_id: str, display: dict, instruments: list[dict], frame: SampledFrame,
                        frame_id: str, ts, state: DisplayState, box: tuple[int, int, int, int] | None) -> dict:
    """Reads (or reuses) one display on one frame. Returns counts and the stored rows."""
    settings, pool = ctx.settings, ctx.pool
    if box is None:
        return {"display_id": display["display_id"], "status": "no_roi", "rows": []}
    signature = await asyncio.to_thread(crop_signature, frame.jpeg, box)

    if (not changed(state.signature, signature, settings.change_threshold)
            and frame.t_offset_s - state.last_read_t < settings.reread_interval_s and state.last):
        stored = 0
        rows = []
        for prev in state.last:
            reading_id = await pool.fetchval(
                _INSERT_READING, ts, new_id("rd_"), prev["instrument_id"], frame_id, prev["value"], prev["unit"],
                prev["raw_text"], prev["display_page"], prev["confidence"], prev["status"], prev["reading_id"],
                prev["crop_uri"], None)
            if reading_id is not None:
                rows.append({**prev, "reading_id": reading_id, "frame_id": frame_id, "reused": True,
                             "origin_id": prev.get("origin_id") or prev["reading_id"]})   # keeps frame_pos too
        return {"display_id": display["display_id"], "status": "reused", "readings": len(rows), "rows": rows}

    crop_png = await asyncio.to_thread(prepare_image, frame.jpeg, box)
    crop_uri = await asyncio.to_thread(ctx.evidence.write, f"{ctx.evidence.clip_dir(clip_id)}/crops/f{frame.index:05d}_{display['display_id']}.png",
                                  crop_png)
    try:
        reading, meta = await read_display(ctx.client, frame.jpeg, context=display["context"] or "", box=box)
    except (ModelUnavailable, StructuredOutputError, CacheMiss) as exc:
        meta = getattr(exc, "meta", None)
        if meta is not None:
            await record_call(pool, meta)
        await emit(pool, clip_id, "read_failed", {"frame_id": frame_id, "display_id": display["display_id"],
                                                  "error": str(exc)[:300]})
        return {"display_id": display["display_id"], "status": "failed", "rows": []}

    await record_call(pool, meta)
    mapping = map_fields(reading, instruments)
    last = []
    for instrument, fld in mapping.mapped:
        confidence = confidence_for(fld, reading)
        status = "needs_review" if confidence == 0.0 else "pending"
        row = {"reading_id": new_id("rd_"), "ts": ts, "instrument_id": instrument["instrument_id"],
               "display_id": display["display_id"],
               "physics_role": instrument["physics_role"], "frame_id": frame_id, "value": fld.value,
               "unit": fld.unit, "raw_text": fld.raw_text, "display_page": reading.page_hint,
               "method": "llm", "confidence": confidence, "status": status, "attempt": 1,
               "crop_uri": crop_uri, "model_call_id": meta.call_id, "resolution": instrument.get("resolution")}
        inserted = await pool.fetchval(
            _INSERT_READING, ts, row["reading_id"], row["instrument_id"], frame_id, fld.value, fld.unit,
            fld.raw_text, reading.page_hint, confidence, status, None, crop_uri, meta.call_id)
        if inserted:
            last.append(row)
            await emit(pool, clip_id, "reading", _reading_event({k: v for k, v in row.items() if k != "resolution"}))
    state.signature, state.last_read_t, state.last = signature, frame.t_offset_s, last
    return {"display_id": display["display_id"], "status": "read", "readings": len(last), "rows": last,
            "unmapped": [f"{f.label}={f.raw_text}{f.unit or ''}" for f in mapping.unmapped],
            "page_hint": reading.page_hint, "cache_hit": meta.cache_hit}


async def process_clip(job, ctx) -> None:
    pool, settings = ctx.pool, ctx.settings
    clip_id = job.payload["clip_id"]
    clip = await pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", clip_id)
    if clip is None:
        raise ValueError(f"clip {clip_id} not found")
    media = clip["media"] or {}
    photos = media.get("kind") == "photos"
    if photos:                                           # each photo is one frame
        frames = await asyncio.to_thread(photo_frames, [(await asyncio.to_thread(ctx.evidence.read, p["uri"]), p["t"]) for p in media["photos"]])
        fps, video_path = None, None
    else:
        video_path = await asyncio.to_thread(ctx.evidence.path, clip["source_uri"])
        fps = await asyncio.to_thread(effective_fps, video_path, settings.sample_fps, settings.max_frames)
        frames = await asyncio.to_thread(sample_frames, video_path, fps)
    if clip["equipment_id"] is None:                     # nobody said what it is: work it out from the video
        if not frames:
            raise ValueError("the video has no frames to identify the equipment from")
        await identify_clip(ctx, clip_id, frames)
        clip = await pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", clip_id)
    displays = [dict(r) for r in await pool.fetch(_DISPLAYS, clip["equipment_id"])]
    if not displays:
        raise ValueError(f"equipment {clip['equipment_id']} has no digital displays registered")
    instruments = [dict(r) for r in await pool.fetch(_INSTRUMENTS, [d["display_id"] for d in displays])]
    by_display = {d["display_id"]: [i for i in instruments if i["display_id"] == d["display_id"]] for d in displays}

    await pool.execute("UPDATE clips SET status = 'processing', fps_sampled = $2 WHERE clip_id = $1",
                       clip_id, fps)
    await emit(pool, clip_id, "job_status", {"status": "processing", "job_id": job.job_id, "attempt": job.attempts})
    done = {r["fid"] for r in await pool.fetch(
        "SELECT payload->>'frame_id' AS fid FROM clip_events WHERE clip_id = $1 AND type = 'frame_processed'", clip_id)}

    states = {d["display_id"]: DisplayState() for d in displays}
    rois = await resolve_rois(ctx, clip_id, displays, frames, video_path, stills=photos) if frames else {}
    physics = await PhysicsStage.load(pool, dict(clip), max_relooks=settings.max_relooks_per_clip,
                                      timed=not photos or bool(media.get("timed")))
    displays_by_id = {d["display_id"]: d for d in displays}
    instruments_by_id = {i["instrument_id"]: i for i in instruments}
    totals = {"frames": len(frames), "read": 0, "reused": 0, "failed": 0, "readings": 0}

    async def save_frame(frame: SampledFrame) -> tuple[str, str]:
        """(frame_id, image_uri). Idempotent: a re-look may need the NEXT frame before the loop reaches it."""
        frame_id = f"{clip_id}_f{frame.index:05d}"
        image_uri = await asyncio.to_thread(ctx.evidence.write, f"{ctx.evidence.clip_dir(clip_id)}/frames/f{frame.index:05d}.jpg",
                                            frame.jpeg)
        await pool.execute(
            """INSERT INTO frames (frame_id, clip_id, t_offset_s, ts, image_uri) VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (frame_id) DO NOTHING""", frame_id, clip_id, frame.t_offset_s,
            clip["recorded_at"] + timedelta(seconds=frame.t_offset_s), image_uri)
        return frame_id, image_uri

    def relooker_for(position: int):
        async def relook(suspect: dict, attempt: int):
            # Re-read the suspect on the frame it was READ from (with cycling pages it may be off screen now):
            # attempt 2: that frame; attempt 3: its neighbour (next, else previous)
            base = suspect.get("frame_pos", position)
            neighbour = base + 1 if base + 1 < len(frames) else max(0, base - 1)
            use = frames[base] if attempt == 2 else frames[neighbour]
            display = displays_by_id.get(suspect.get("display_id"))
            box = rois.get(suspect.get("display_id"), {}).get(use.index)
            instrument = instruments_by_id.get(suspect["instrument_id"])
            if display is None or box is None or instrument is None:
                return None
            await save_frame(use)                       # its readings reference the frame row
            result = await look_again(ctx, clip_id, use, display, box, instrument, suspect, attempt)
            if result is not None:                      # the re-read belongs to the frame it was read from
                result.detail["frame_id"] = f"{clip_id}_f{use.index:05d}"
                result.detail["t_offset_s"] = use.t_offset_s
            return result
        return relook

    for position, frame in enumerate(frames):
        frame_id = f"{clip_id}_f{frame.index:05d}"
        ts = clip["recorded_at"] + timedelta(seconds=frame.t_offset_s)
        if frame_id in done:
            continue
        _, image_uri = await save_frame(frame)
        results = await asyncio.gather(*(_read_display(ctx, clip_id, d, by_display[d["display_id"]], frame,
                                                       frame_id, ts, states[d["display_id"]],
                                                       rois.get(d["display_id"], {}).get(frame.index))
                                         for d in displays))
        rows = [row for r in results for row in r.pop("rows", [])]
        for row in rows:
            if not row.get("reused"):
                row["frame_pos"] = position                # where to re-look it later, if it becomes a suspect
        fresh_roles = {row["physics_role"] for row in rows if not row.get("reused")}
        checks, corrections = (await physics.observe(frame_id, frame.t_offset_s, rows, fresh_roles,
                                                     relooker=relooker_for(position))
                               if physics else ([], []))
        for c in corrections:    # later frames that reuse this crop carry the corrected value, or the new status
            last = states[c["display_id"]].last if c["display_id"] in states else []
            for i, prev in enumerate(last):
                if prev["instrument_id"] != c["instrument_id"]:
                    continue
                if "row" in c:
                    last[i] = {**prev, **{k: c["row"][k] for k in ("reading_id", "value", "raw_text", "status",
                                                                     "crop_uri", "confidence")}}
                else:
                    last[i] = {**prev, "status": c["status"]}
        for r in results:
            totals[r["status"]] = totals.get(r["status"], 0) + 1
            totals["readings"] += r.get("readings", 0)
        await emit(pool, clip_id, "frame_processed", {"frame_id": frame_id, "index": frame.index,
                                                      "t_offset_s": frame.t_offset_s, "image_uri": image_uri,
                                                      "displays": results, "physics": checks})
        await ctx.progress((frame.index + 1) / max(1, len(frames)))

    if physics:
        totals.update(physics.totals)
    if physics:
        for anomaly_id in physics.anomalies.values():           # each confirmed fault gets a diagnosis
            await ctx.queue.enqueue("diagnose", {"anomaly_id": anomaly_id, "clip_id": clip_id},
                                    dedupe_key=f"diagnose:{anomaly_id}")
    await pool.execute("UPDATE clips SET status = 'done' WHERE clip_id = $1", clip_id)
    await emit(pool, clip_id, "clip_done", totals)
    log.info("clip %s done: %s", clip_id, totals)
