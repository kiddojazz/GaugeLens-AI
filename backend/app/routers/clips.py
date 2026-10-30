"""Clips, live clip events (SSE), readings, equipment registry and job retry.
Every route needs a session (access.py) and shows only the caller's clips; admins see all of them.

    POST /api/v1/clips                     multipart: file (one video, or 1-10 photos), equipment_id, recorded_at?
    GET  /api/v1/clips/{clip_id}           clip + job status + counts
    GET  /api/v1/clips/{clip_id}/events    SSE stream from clip_events (Last-Event-ID / ?after= resume)
    GET  /api/v1/readings                  ?clip_id= | ?instrument_id= [&limit=]
    GET  /api/v1/equipment                 equipment with displays and instruments
    POST /api/v1/jobs/{job_id}/retry       re-queue a dead job
    GET  /api/v1/clips                     recent clips with counts
    GET  /api/v1/clips/{clip_id}/frames    frames + each display's box on each frame (tracked or static) + values
                                           read there but not matched to a registered quantity (unmapped)
    GET  /api/v1/clips/{clip_id}/checks    physics checks (?include_skipped=true for coverage)
    GET  /api/v1/corrections               misreads caught by physics and fixed by the re-look (attempt 1 vs 2)
    GET  /api/v1/evidence?uri=             an evidence file (frame, crop) from the evidence store
"""

import asyncio
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..access import Principal, catalogue_or_visible, current_principal, require_clip, require_evidence, visible
from ..jobs.events import fetch_after
from ..limits import caller_key, enforce
from ..pipeline.clips import ClipError, create_clip, create_photo_clip, is_photo

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
async def upload_clip(request: Request, file: list[UploadFile] = File(...), equipment_id: str = Form("auto"),
                      recorded_at: datetime | None = Form(None), simulated: bool = Form(False),
                      principal: Principal = Depends(current_principal)):
    state = _state(request)
    if principal.kind == "guest" and await state.pool.fetchval(
            "SELECT count(*) FROM clips WHERE owner_session_id = $1 AND owner_user_id IS NULL",
            principal.session_id) >= state.settings.guest_max_clips:
        raise HTTPException(status_code=429, detail=f"guests can scan {state.settings.guest_max_clips} clips per visit; "
                                                    "create a free account to scan more")
    if not principal.is_admin:
        key, is_guest = caller_key(principal, request)
        enforce(request, f"{key}:uploads", state.settings.guest_uploads_per_hour if is_guest
                else state.settings.user_uploads_per_hour, "uploads")
    equipment_id = None if equipment_id in ("", "auto") else equipment_id
    if equipment_id and not await state.pool.fetchval(
            f"SELECT 1 FROM equipment e WHERE e.equipment_id = $1 AND {catalogue_or_visible('e', 2)}",
            equipment_id, *principal.scope()):
        raise HTTPException(status_code=400, detail=f"unknown equipment '{equipment_id}'")
    async with request.app.state.upload_gate:
        limit = state.settings.max_upload_mb * 1024 * 1024
        uploads, total = [], 0
        for f in file:
            data = await f.read(limit + 1 - total)
            total += len(data)
            if total > limit:
                raise HTTPException(status_code=413, detail=f"upload larger than {state.settings.max_upload_mb} MB; "
                                                                "record at 1080p and under 2 minutes")
            uploads.append((f.filename or "upload", data))
        photos = [is_photo(name) for name, _ in uploads]
        try:
            if all(photos):
                clip_id, job_id = await create_photo_clip(state.pool, state.evidence, state.queue, photos=uploads,
                                                          equipment_id=equipment_id, is_demo_replay=simulated,
                                                          owner=principal.owner())
            elif len(uploads) == 1:
                clip_id, job_id = await create_clip(state.pool, state.evidence, state.queue, video=uploads[0][1],
                                                    filename=uploads[0][0], equipment_id=equipment_id,
                                                    recorded_at=recorded_at, is_demo_replay=simulated,
                                                    owner=principal.owner())
            else:
                raise ClipError("send one video, or up to 10 photos (not both)")
        except ClipError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"clip_id": clip_id, "job_id": job_id, "status": "queued"}


@router.get("/clips/{clip_id}")
async def get_clip(request: Request, clip_id: str, principal: Principal = Depends(current_principal)):
    state = _state(request)
    await require_clip(state.pool, principal, clip_id)
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
async def clip_events(request: Request, clip_id: str, after: int = 0, follow: bool = True,
                      principal: Principal = Depends(current_principal)):
    """Server-Sent Events. Polls clip_events about once a second; pings every 15 s to keep proxies open.
    EventSource can't send headers, so a guest passes the session as ?s= (access.py)."""
    state = _state(request)
    await require_clip(state.pool, principal, clip_id)
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
                        limit: int = Query(500, le=5000), principal: Principal = Depends(current_principal)):
    state = _state(request)
    if not clip_id and not instrument_id:
        raise HTTPException(status_code=400, detail="give clip_id or instrument_id")
    if clip_id:
        await require_clip(state.pool, principal, clip_id)
    elif not principal.is_admin:                        # an instrument's history spans other people's clips
        raise HTTPException(status_code=400, detail="give clip_id")
    rows = await state.pool.fetch(
        """SELECT r.ts, r.reading_id, r.instrument_id, i.physics_role, r.frame_id, f.t_offset_s, r.value, r.unit,
                  r.raw_text, r.display_page, r.method, r.confidence, r.status, r.attempt, r.supersedes,
                  r.reused_from, r.crop_uri, r.model_call_id
           FROM readings r JOIN frames f ON f.frame_id = r.frame_id
           LEFT JOIN instruments i ON i.instrument_id = r.instrument_id
           WHERE ($1::text IS NULL OR f.clip_id = $1) AND ($2::text IS NULL OR r.instrument_id = $2)
           ORDER BY r.ts, r.instrument_id LIMIT $3""", clip_id, instrument_id, limit)
    return [_jsonable(dict(r)) for r in rows]


class EquipmentCorrection(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    kind: Literal["inverter", "charge_controller", "generator", "meter", "clamp_meter", "multimeter",
                  "pressure_system", "other"] | None = None


@router.patch("/equipment/{equipment_id}")
async def correct_equipment(request: Request, equipment_id: str, body: EquipmentCorrection,
                            principal: Principal = Depends(current_principal)):
    """A person corrects what GaugeLens identified (name, type). Applies to every later clip of this unit.
    Only the unit's owner (or an admin) can; the shared catalogue is admin-only."""
    state = _state(request)
    row = await state.pool.fetchrow(
        f"""UPDATE equipment e SET model = COALESCE($2, model), kind = COALESCE($3, kind),
               profile = COALESCE(profile, '{{}}'::jsonb) || jsonb_build_object('corrected', true, 'corrected_at', now())
           WHERE equipment_id = $1 AND {visible('e', 4)} RETURNING *""", equipment_id, body.model, body.kind,
        *principal.scope())
    if row is None:
        raise HTTPException(status_code=404, detail="equipment not found")
    return _jsonable(dict(row))


@router.get("/equipment")
async def list_equipment(request: Request, principal: Principal = Depends(current_principal)):
    """The caller's own units, the units in clips they can see, and the shared catalogue."""
    state = _state(request)
    equipment = [dict(r) for r in await state.pool.fetch(
        f"""SELECT * FROM equipment e WHERE {catalogue_or_visible('e', 1)}
              OR EXISTS (SELECT 1 FROM clips c WHERE c.equipment_id = e.equipment_id AND {visible('c', 1)})
            ORDER BY equipment_id""", *principal.scope())]
    displays = [dict(r) for r in await state.pool.fetch("SELECT * FROM displays ORDER BY display_id")]
    instruments = [dict(r) for r in await state.pool.fetch("SELECT * FROM instruments ORDER BY instrument_id")]
    for e in equipment:
        e["displays"] = [_jsonable(d) for d in displays if d["equipment_id"] == e["equipment_id"]]
        for d in e["displays"]:
            d["instruments"] = [_jsonable(i) for i in instruments if i["display_id"] == d["display_id"]]
    return [_jsonable(e) for e in equipment]


@router.get("/clips")
async def list_clips(request: Request, limit: int = Query(50, le=200),
                     principal: Principal = Depends(current_principal)):
    state = _state(request)
    rows = await state.pool.fetch(
        f"""SELECT c.clip_id, c.equipment_id, e.kind AS equipment_kind, c.status, c.source_channel, c.is_demo_replay,
                  c.created_at, CASE WHEN $2 THEN COALESCE(u.email, CASE WHEN c.owner_session_id IS NOT NULL
                                                      THEN 'guest' END) END AS owner,
                  (SELECT count(*) FROM frames f WHERE f.clip_id = c.clip_id) AS frames,
                  (SELECT count(*) FROM anomalies a WHERE a.clip_id = c.clip_id) AS anomalies,
                  (SELECT count(*) FROM physics_checks p WHERE p.clip_id = c.clip_id AND p.outcome = 'misread_fixed') AS corrections,
                  (SELECT progress FROM jobs j WHERE j.dedupe_key = 'process_clip:' || c.clip_id) AS progress
           FROM clips c LEFT JOIN equipment e USING (equipment_id) LEFT JOIN users u ON u.user_id = c.owner_user_id
           WHERE c.source_channel <> 'eval'                     -- evaluation scenarios live on the Evaluations page
             AND {visible('c', 2)}
           ORDER BY c.created_at DESC LIMIT $1""", limit, *principal.scope())
    return [_jsonable(dict(r)) for r in rows]


@router.get("/clips/{clip_id}/frames")
async def clip_frames(request: Request, clip_id: str, principal: Principal = Depends(current_principal)):
    state = _state(request)
    await require_clip(state.pool, principal, clip_id)
    frames = await state.pool.fetch(
        "SELECT frame_id, t_offset_s, image_uri FROM frames WHERE clip_id = $1 ORDER BY t_offset_s", clip_id)
    rois = await state.pool.fetch(
        """SELECT r.display_id, d.label, r.roi, r.frame_size, r.source, r.confidence, r.track
           FROM clip_rois r JOIN displays d USING (display_id) WHERE r.clip_id = $1""", clip_id)
    unmapped: dict[str, list[str]] = {}       # e.g. "energy=002320kWh" with no register code: import or export?
    for e in await state.pool.fetch(
            """SELECT payload->>'frame_id' AS frame_id, payload->'displays' AS displays FROM clip_events
               WHERE clip_id = $1 AND type = 'frame_processed'""", clip_id):
        unmapped[e["frame_id"]] = [u for d in (e["displays"] or []) for u in (d.get("unmapped") or [])]
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
                    "boxes": boxes, "unmapped": unmapped.get(f["frame_id"], [])})
    size = rois[0]["frame_size"] if rois else None
    return {"clip_id": clip_id, "frame_size": size, "frames": out}


@router.get("/clips/{clip_id}/checks")
async def clip_checks(request: Request, clip_id: str, include_skipped: bool = False,
                      principal: Principal = Depends(current_principal)):
    state = _state(request)
    await require_clip(state.pool, principal, clip_id)
    rows = await state.pool.fetch(
        """SELECT pc.check_id, pc.rule_id, pc.outcome, pc.passed, pc.expected, pc.observed, pc.residual_pct,
                  pc.tolerance_used, pc.reason, pc.reading_ids, pc.suspect_reading_id, pc.candidate_fix, pc.relook,
                  pc.frame_id, f.t_offset_s
           FROM physics_checks pc LEFT JOIN frames f USING (frame_id)
           WHERE pc.clip_id = $1 AND ($2 OR pc.outcome <> 'skipped') ORDER BY pc.ts, pc.rule_id""",
        clip_id, include_skipped)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/corrections")
async def corrections(request: Request, clip_id: str | None = None, limit: int = Query(50, le=200),
                      principal: Principal = Depends(current_principal)):
    """Each misread fixed by the re-look, with both crops: what was read first and what was re-read."""
    state = _state(request)
    rows = await state.pool.fetch(
        f"""SELECT DISTINCT ON (pc.suspect_reading_id) pc.check_id, pc.clip_id, pc.rule_id, pc.relook, pc.frame_id,
                  f.t_offset_s, r.raw_text AS original_text, r.crop_uri AS original_crop, i.physics_role, i.unit,
                  pc.ts
           FROM physics_checks pc JOIN readings r ON r.reading_id = pc.suspect_reading_id
           JOIN instruments i ON i.instrument_id = r.instrument_id LEFT JOIN frames f ON f.frame_id = pc.frame_id
           JOIN clips c ON c.clip_id = pc.clip_id
           WHERE pc.outcome = 'misread_fixed' AND ($1::text IS NULL OR pc.clip_id = $1) AND {visible('c', 2)}
           ORDER BY pc.suspect_reading_id, pc.ts""", clip_id, *principal.scope())
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
async def evidence_file(request: Request, uri: str, principal: Principal = Depends(current_principal)):
    """<img> and <video> can't send headers, so a guest passes the session as ?s= (access.py)."""
    state = _state(request)
    await require_evidence(state.pool, principal, uri)
    try:
        path = await asyncio.to_thread(state.evidence.path, uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="evidence not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str, principal: Principal = Depends(current_principal)):
    state = _state(request)
    clip_id = await state.pool.fetchval("SELECT payload->>'clip_id' FROM jobs WHERE job_id = $1", job_id)
    if clip_id:
        await require_clip(state.pool, principal, clip_id)
    elif not principal.is_admin:
        raise HTTPException(status_code=404, detail="job not found")
    if not await state.queue.retry(job_id):
        raise HTTPException(status_code=409, detail="job is not dead or failed")
    return {"job_id": job_id, "status": "queued"}
