"""Anomalies, diagnoses and human decisions.

    GET  /api/v1/anomalies?clip_id=&equipment_id=   anomalies with their diagnosis status
    POST /api/v1/anomalies/{id}/diagnose            queue the diagnosis agent (idempotent)
    GET  /api/v1/anomalies/{id}/diagnosis           the diagnosis for an anomaly
    GET  /api/v1/diagnoses/{id}
    POST /api/v1/diagnoses/{id}/decision            {"decision": "approved"|"rejected", "note"?, "decided_by"?}
Nothing acts on equipment: a decision is only recorded (and later sent back on WhatsApp).
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..jobs.events import emit
from .clips import _jsonable, _state

router = APIRouter(prefix="/api/v1")


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)
    decided_by: str = Field(default="web", max_length=200)


@router.get("/anomalies")
async def list_anomalies(request: Request, clip_id: str | None = None, equipment_id: str | None = None):
    state = _state(request)
    rows = await state.pool.fetch(
        """SELECT a.*, d.diagnosis_id, d.verified, d.decision, d.output->>'headline' AS headline FROM anomalies a
           LEFT JOIN diagnoses d USING (anomaly_id)
           WHERE ($1::text IS NULL OR a.clip_id = $1) AND ($2::text IS NULL OR a.equipment_id = $2)
             AND ($1::text IS NOT NULL OR NOT EXISTS (       -- eval scenarios stay out of the incident list
                 SELECT 1 FROM clips c WHERE c.clip_id = a.clip_id AND c.source_channel = 'eval'))
           ORDER BY a.ts_start DESC LIMIT 200""", clip_id, equipment_id)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/anomalies/{anomaly_id}")
async def get_anomaly(request: Request, anomaly_id: str):
    state = _state(request)
    row = await state.pool.fetchrow(
        """SELECT a.*, e.kind AS equipment_kind, e.model AS equipment_model, d.diagnosis_id, d.verified, d.decision,
                  d.output->>'headline' AS headline,
                  (SELECT status FROM jobs j WHERE j.dedupe_key = 'diagnose:' || a.anomaly_id) AS diagnose_job
           FROM anomalies a LEFT JOIN equipment e USING (equipment_id) LEFT JOIN diagnoses d USING (anomaly_id)
           WHERE a.anomaly_id = $1""", anomaly_id)
    if row is None:
        raise HTTPException(status_code=404, detail="anomaly not found")
    return _jsonable(dict(row))


@router.post("/anomalies/{anomaly_id}/diagnose", status_code=202)
async def diagnose(request: Request, anomaly_id: str):
    state = _state(request)
    anomaly = await state.pool.fetchrow("SELECT clip_id FROM anomalies WHERE anomaly_id = $1", anomaly_id)
    if anomaly is None:
        raise HTTPException(status_code=404, detail="anomaly not found")
    job_id = await state.queue.enqueue("diagnose", {"anomaly_id": anomaly_id, "clip_id": anomaly["clip_id"]},
                                       dedupe_key=f"diagnose:{anomaly_id}")
    return {"anomaly_id": anomaly_id, "job_id": job_id}


async def _diagnosis(pool, where: str, value: str) -> dict:
    row = await pool.fetchrow(f"SELECT * FROM diagnoses WHERE {where} = $1", value)
    if row is None:
        raise HTTPException(status_code=404, detail="diagnosis not found")
    return _jsonable(dict(row))


@router.get("/anomalies/{anomaly_id}/diagnosis")
async def diagnosis_for_anomaly(request: Request, anomaly_id: str):
    return await _diagnosis(_state(request).pool, "anomaly_id", anomaly_id)


@router.get("/diagnoses/{diagnosis_id}")
async def get_diagnosis(request: Request, diagnosis_id: str):
    return await _diagnosis(_state(request).pool, "diagnosis_id", diagnosis_id)


@router.post("/diagnoses/{diagnosis_id}/decision")
async def decide(request: Request, diagnosis_id: str, body: Decision):
    state = _state(request)
    row = await state.pool.fetchrow(
        """UPDATE diagnoses SET decision = $2, decided_by = $3, decided_at = $4, decision_note = $5
           WHERE diagnosis_id = $1 AND decision = 'pending'
           RETURNING diagnosis_id, anomaly_id, decision, decided_by, decided_at""",
        diagnosis_id, body.decision, body.decided_by, datetime.now(timezone.utc), body.note)
    if row is None:
        exists = await state.pool.fetchval("SELECT decision FROM diagnoses WHERE diagnosis_id = $1", diagnosis_id)
        raise HTTPException(status_code=404 if exists is None else 409,
                            detail="diagnosis not found" if exists is None else f"already {exists}")
    clip_id = await state.pool.fetchval("SELECT clip_id FROM anomalies WHERE anomaly_id = $1", row["anomaly_id"])
    if clip_id:
        await emit(state.pool, clip_id, "diagnosis", {"diagnosis_id": diagnosis_id, "decision": body.decision,
                                                      "decided_by": body.decided_by})
    return _jsonable(dict(row))
