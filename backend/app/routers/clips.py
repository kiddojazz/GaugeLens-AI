"""Clips, live clip events (SSE), readings, equipment registry and job retry.

    POST /api/v1/clips                     multipart: file, equipment_id, recorded_at? -> {clip_id, job_id}
    GET  /api/v1/clips/{clip_id}           clip + job status + counts
    GET  /api/v1/clips/{clip_id}/events    SSE stream from clip_events (Last-Event-ID / ?after= resume)
    GET  /api/v1/readings                  ?clip_id= | ?instrument_id= [&limit=]
    GET  /api/v1/equipment                 equipment with displays and instruments
    POST /api/v1/jobs/{job_id}/retry       re-queue a dead job
    GET  /api/v1/clips                     recent clips with counts
    GET  /api/v1/clips/{clip_id}/frames    frames + each display's box on each frame (tracked or static)
    GET  /api/v1/clips/{clip_id}/checks    physics checks (?include_skipped=true for coverage)
    GET  /api/v1/corrections               misreads caught by physics and fixed by the re-look (attempt 1 vs 2)
    GET  /api/v1/evidence?uri=             an evidence file (frame, crop) from the evidence store
"""

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from ..jobs.events import fetch_after
from ..pipeline.clips import ClipError, create_clip

router = APIRouter(prefix="/api/v1")


def _state(request: Request):
    state = request.app.state
    if getattr(state, "pool", None) is None:
        raise HTTPException(status_code=503, detail="database not available")
    return state


def _jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else float(v) if type(v).__name__ == "Decimal" else v)
            for k, v in row.items()}


@router.post("/clips", status_code=201)
async def upload_clip(request: Request, file: UploadFile = File(...), equipment_id: str = Form(...),
                      recorded_at: datetime | None = Form(None)):
    state = _state(request)
    limit = state.settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"video larger than {state.settings.max_upload_mb} MB")
    try:
        clip_id, job_id = await create_clip(state.pool, state.evidence, state.queue, video=data,
                                            filename=file.filename or "upload", equipment_id=equipment_id,
                                            recorded_at=recorded_at)
    except ClipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"clip_id": clip_id, "job_id": job_id, "status": "queued"}


@router.get("/clips/{clip_id}")
async def get_clip(request: Request, clip_id: str):
    state = _state(request)
    clip = await state.pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="clip not found")
    job = await state.pool.fetchrow(
        "SELECT job_id, status, attempts, progress, last_error FROM jobs WHERE dedupe_key = $1",
        f"process_clip:{clip_id}")
    counts = await state.pool.fetchrow(
        """SELECT (SELECT count(*) FROM frames WHERE clip_id = $1) AS frames,
                  (SELECT count(*) FROM readings r JOIN frames f ON f.frame_id = r.frame_id WHERE f.clip_id = $1) AS readings""",
        clip_id)
    return {"clip": _jsonable(dict(clip)), "job": _jsonable(dict(job)) if job else None, "counts": dict(counts)}


@router.get("/clips/{clip_id}/events")
async def clip_events(request: Request, clip_id: str, after: int = 0, follow: bool = True):
    """Server-Sent Events. Polls clip_events about once a second; pings every 15 s to keep proxies open."""
    state = _state(request)
    last_id = int(request.headers.get("last-event-id") or after)

    async def stream():
        nonlocal last_id
        idle = 0
        while True:
            rows = await fetch_after(state.pool, clip_id, last_id)
            for row in rows:
                last_id = row["event_id"]
                payload = {**row["payload"], "ts": row["ts"].isoformat()}
                yield f"id: {last_id}\nevent: {row['type']}\ndata: {json.dumps(payload, default=str)}\n\n"
            if not follow and len(rows) < 200:
                return
            if await request.is_disconnected():
                return
            idle = 0 if rows else idle + 1
            if idle and idle % 15 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/readings")
async def list_readings(request: Request, clip_id: str | None = None, instrument_id: str | None = None,
                        limit: int = Query(500, le=5000)):
    state = _state(request)
    if not clip_id and not instrument_id:
        raise HTTPException(status_code=400, detail="give clip_id or instrument_id")
    rows = await state.pool.fetch(
        """SELECT r.ts, r.reading_id, r.instrument_id, i.physics_role, r.frame_id, f.t_offset_s, r.value, r.unit,
                  r.raw_text, r.display_page, r.method, r.confidence, r.status, r.attempt, r.supersedes,
                  r.reused_from, r.crop_uri, r.model_call_id
           FROM readings r JOIN frames f ON f.frame_id = r.frame_id
           LEFT JOIN instruments i ON i.instrument_id = r.instrument_id
           WHERE ($1::text IS NULL OR f.clip_id = $1) AND ($2::text IS NULL OR r.instrument_id = $2)
           ORDER BY r.ts, r.instrument_id LIMIT $3""", clip_id, instrument_id, limit)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/equipment")
async def list_equipment(request: Request):
    state = _state(request)
    equipment = [dict(r) for r in await state.pool.fetch("SELECT * FROM equipment ORDER BY equipment_id")]
    displays = [dict(r) for r in await state.pool.fetch("SELECT * FROM displays ORDER BY display_id")]
    instruments = [dict(r) for r in await state.pool.fetch("SELECT * FROM instruments ORDER BY instrument_id")]
    for e in equipment:
        e["displays"] = [_jsonable(d) for d in displays if d["equipment_id"] == e["equipment_id"]]
        for d in e["displays"]:
            d["instruments"] = [_jsonable(i) for i in instruments if i["display_id"] == d["display_id"]]
    return [_jsonable(e) for e in equipment]


@router.get("/clips")
async def list_clips(request: Request, limit: int = Query(50, le=200)):
    state = _state(request)
    rows = await state.pool.fetch(
        """SELECT c.clip_id, c.equipment_id, e.kind AS equipment_kind, c.status, c.source_channel, c.is_demo_replay,
                  c.created_at,
                  (SELECT count(*) FROM frames f WHERE f.clip_id = c.clip_id) AS frames,
                  (SELECT count(*) FROM anomalies a WHERE a.clip_id = c.clip_id) AS anomalies,
                  (SELECT count(*) FROM physics_checks p WHERE p.clip_id = c.clip_id AND p.outcome = 'misread_fixed') AS corrections,
                  (SELECT progress FROM jobs j WHERE j.dedupe_key = 'process_clip:' || c.clip_id) AS progress
           FROM clips c LEFT JOIN equipment e USING (equipment_id)
           WHERE c.source_channel <> 'eval'                     -- evaluation scenarios live on the Evaluations page
           ORDER BY c.created_at DESC LIMIT $1""", limit)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/clips/{clip_id}/frames")
async def clip_frames(request: Request, clip_id: str):
    state = _state(request)
    frames = await state.pool.fetch(
        "SELECT frame_id, t_offset_s, image_uri FROM frames WHERE clip_id = $1 ORDER BY t_offset_s", clip_id)
    rois = await state.pool.fetch(
        """SELECT r.display_id, d.label, r.roi, r.frame_size, r.source, r.confidence, r.track
           FROM clip_rois r JOIN displays d USING (display_id) WHERE r.clip_id = $1""", clip_id)
    out = []
    for f in frames:
        index = int(f["frame_id"].rsplit("_f", 1)[1])
        boxes = []
        for r in rois:
            static = [r["roi"]["x"], r["roi"]["y"], r["roi"]["w"], r["roi"]["h"]]
            box = (r["track"] or {}).get(str(index), static)
            boxes.append({"display_id": r["display_id"], "label": r["label"], "box": box, "source": r["source"],
                          "tracked": r["track"] is not None})
        out.append({"frame_id": f["frame_id"], "index": index, "t": float(f["t_offset_s"]), "image_uri": f["image_uri"],
                    "boxes": boxes})
    size = rois[0]["frame_size"] if rois else None
    return {"clip_id": clip_id, "frame_size": size, "frames": out}


@router.get("/clips/{clip_id}/checks")
async def clip_checks(request: Request, clip_id: str, include_skipped: bool = False):
    state = _state(request)
    rows = await state.pool.fetch(
        """SELECT pc.check_id, pc.rule_id, pc.outcome, pc.passed, pc.expected, pc.observed, pc.residual_pct,
                  pc.tolerance_used, pc.reason, pc.reading_ids, pc.suspect_reading_id, pc.candidate_fix, pc.relook,
                  pc.frame_id, f.t_offset_s
           FROM physics_checks pc LEFT JOIN frames f USING (frame_id)
           WHERE pc.clip_id = $1 AND ($2 OR pc.outcome <> 'skipped') ORDER BY pc.ts, pc.rule_id""",
        clip_id, include_skipped)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/corrections")
async def corrections(request: Request, clip_id: str | None = None, limit: int = Query(50, le=200)):
    """Each misread fixed by the re-look, with both crops: what was read first and what was re-read."""
    state = _state(request)
    rows = await state.pool.fetch(
        """SELECT DISTINCT ON (pc.suspect_reading_id) pc.check_id, pc.clip_id, pc.rule_id, pc.relook, pc.frame_id,
                  f.t_offset_s, r.raw_text AS original_text, r.crop_uri AS original_crop, i.physics_role, i.unit,
                  pc.ts
           FROM physics_checks pc JOIN readings r ON r.reading_id = pc.suspect_reading_id
           JOIN instruments i ON i.instrument_id = r.instrument_id LEFT JOIN frames f ON f.frame_id = pc.frame_id
           WHERE pc.outcome = 'misread_fixed' AND ($1::text IS NULL OR pc.clip_id = $1)
           ORDER BY pc.suspect_reading_id, pc.ts""", clip_id)
    out = []
    for r in sorted(rows, key=lambda x: x["ts"], reverse=True)[:limit]:
        fixed = next((a for a in (r["relook"] or {}).get("attempts", []) if a.get("outcome") == "fixed"), None)
        out.append(_jsonable({"check_id": r["check_id"], "clip_id": r["clip_id"], "rule_id": r["rule_id"],
                              "frame_id": r["frame_id"], "t_offset_s": r["t_offset_s"], "role": r["physics_role"],
                              "unit": r["unit"], "original_text": r["original_text"], "original_crop": r["original_crop"],
                              "fixed_text": fixed and fixed.get("raw_text"), "fixed_crop": fixed and fixed.get("crop_uri"),
                              "strategy": fixed and fixed.get("strategy"), "ts": r["ts"]}))
    return out


@router.get("/evidence")
async def evidence_file(request: Request, uri: str):
    state = _state(request)
    try:
        path = state.evidence.path(uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="evidence not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str):
    state = _state(request)
    if not await state.queue.retry(job_id):
        raise HTTPException(status_code=409, detail="job is not dead or failed")
    return {"job_id": job_id, "status": "queued"}
