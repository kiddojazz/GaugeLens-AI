"""Agent traces and evaluations.

Traces and evaluations are admin pages; anyone may read the traces of their own clips' anomalies and chats.

    GET  /api/v1/traces?anomaly_id=&eval_run_id=&limit=   agent runs, newest first
    GET  /api/v1/traces/{run_id}                          one run with all its spans (the step tree)
    GET  /api/v1/evals                                    evaluation runs, newest first
    GET  /api/v1/evals/scenarios                          the scenario catalog
    GET  /api/v1/evals/{eval_run_id}                      one evaluation with per-scenario results
    POST /api/v1/evals                                    {"scenarios"?: [...]} queue an evaluation (uses model credits)
    GET  /api/v1/clips/{clip_id}/chat                     the clip's conversation
    POST /api/v1/clips/{clip_id}/chat                     {"question", "frame_id"?, "anomaly_id"?} -> the answer
    DELETE /api/v1/clips/{clip_id}/chat                   start a new conversation (earlier turns are kept, hidden)
"""

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..access import (Principal, admin_principal, can_edit_clip, current_principal, require_anomaly, require_clip,
                      visible)
from ..agent import chat
from ..limits import caller_key, enforce

from ..agent.evals import BY_ID, scenario_catalog
from ..ids import new_id
from .clips import _jsonable, _state

router = APIRouter(prefix="/api/v1")

_RUN_COLUMNS = """r.run_id, r.kind, r.anomaly_id, r.diagnosis_id, r.eval_run_id, r.scenario_id, r.model, r.prompt_version,
                  r.status, r.verified, r.started_at, r.ended_at, r.duration_ms, r.llm_calls, r.tool_calls,
                  r.input_tokens, r.output_tokens, r.error, a.rule_id, a.equipment_id,
                  COALESCE(a.clip_id, r.clip_id) AS clip_id, r.title,
                  COALESCE(d.output->>'headline', r.title) AS headline"""


async def _owns_anomaly(pool, principal: Principal, anomaly_id: str) -> bool:
    clip_id = await pool.fetchval("SELECT clip_id FROM anomalies WHERE anomaly_id = $1", anomaly_id)
    return bool(clip_id) and await can_edit_clip(pool, principal, clip_id)


@router.get("/traces")
async def list_traces(request: Request, anomaly_id: str | None = None, eval_run_id: str | None = None,
                      limit: int = Query(50, le=200), principal: Principal = Depends(current_principal)):
    state = _state(request)
    own = principal.is_admin
    if anomaly_id:
        await require_anomaly(state.pool, principal, anomaly_id)
        own = own or await _owns_anomaly(state.pool, principal, anomaly_id)
    elif not principal.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    rows = await state.pool.fetch(
        f"""SELECT {_RUN_COLUMNS} FROM agent_runs r
            LEFT JOIN anomalies a USING (anomaly_id) LEFT JOIN diagnoses d ON d.diagnosis_id = r.diagnosis_id
            WHERE ($1::text IS NULL OR r.anomaly_id = $1) AND ($2::text IS NULL OR r.eval_run_id = $2)
              AND ($4 OR r.kind <> 'chat')                 -- on an example clip, other visitors' questions stay theirs
            ORDER BY r.started_at DESC LIMIT $3""", anomaly_id, eval_run_id, limit, own)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/traces/{run_id}")
async def get_trace(request: Request, run_id: str, principal: Principal = Depends(current_principal)):
    state = _state(request)
    run = await state.pool.fetchrow(
        f"""SELECT {_RUN_COLUMNS} FROM agent_runs r
            LEFT JOIN anomalies a USING (anomaly_id) LEFT JOIN diagnoses d ON d.diagnosis_id = r.diagnosis_id
            LEFT JOIN clips c ON c.clip_id = COALESCE(a.clip_id, r.clip_id)
            WHERE r.run_id = $1 AND ({visible('c', 2)} OR (c.is_public AND r.kind <> 'chat'))""",
        run_id, *principal.scope())
    if run is None:
        raise HTTPException(status_code=404, detail="trace not found")
    spans = await state.pool.fetch(
        """SELECT span_id, parent_id, seq, name, kind, status, started_at, ended_at, duration_ms, attributes, input, output
           FROM agent_spans WHERE run_id = $1 ORDER BY seq""", run_id)
    return _jsonable({"run": dict(run), "spans": [dict(s) for s in spans]})


@router.get("/evals")
async def list_evals(request: Request, limit: int = Query(20, le=100), _: Principal = Depends(admin_principal)):
    state = _state(request)
    rows = await state.pool.fetch(
        """SELECT e.*, (SELECT progress FROM jobs j WHERE j.dedupe_key = 'run_eval:' || e.eval_run_id) AS progress
           FROM eval_runs e ORDER BY e.started_at DESC LIMIT $1""", limit)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/evals/scenarios")
async def list_scenarios(_: Principal = Depends(admin_principal)):
    return scenario_catalog()


@router.get("/evals/{eval_run_id}")
async def get_eval(request: Request, eval_run_id: str, _: Principal = Depends(admin_principal)):
    state = _state(request)
    run = await state.pool.fetchrow(
        """SELECT e.*, (SELECT progress FROM jobs j WHERE j.dedupe_key = 'run_eval:' || e.eval_run_id) AS progress
           FROM eval_runs e WHERE eval_run_id = $1""", eval_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="evaluation not found")
    results = await state.pool.fetch(
        """SELECT er.*, r.duration_ms AS agent_ms, r.tool_calls, r.llm_calls, r.input_tokens, r.output_tokens
           FROM eval_results er LEFT JOIN agent_runs r USING (run_id)
           WHERE er.eval_run_id = $1""", eval_run_id)
    order = {s["id"]: n for n, s in enumerate(scenario_catalog())}
    results = sorted((dict(r) for r in results), key=lambda r: order.get(r["scenario_id"], 99))
    return _jsonable({"run": dict(run), "results": results})


class EvalRequest(BaseModel):
    scenarios: list[str] | None = None


@router.post("/evals", status_code=202)
async def start_eval(request: Request, body: EvalRequest | None = None, _: Principal = Depends(admin_principal)):
    state = _state(request)
    if state.queue is None:
        raise HTTPException(status_code=503, detail="database not available")
    scenarios = (body.scenarios if body else None) or None
    unknown = [s for s in scenarios or [] if s not in BY_ID]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown scenarios: {', '.join(unknown)}")
    running = await state.pool.fetchval(                 # a run older than 30 min died with its server
        "SELECT eval_run_id FROM eval_runs WHERE status IN ('queued', 'running') "
        "AND started_at > now() - interval '30 minutes' LIMIT 1")
    if running:
        raise HTTPException(status_code=409, detail=f"evaluation {running} is still running")
    eval_run_id = new_id("ev_")
    await state.pool.execute(
        "INSERT INTO eval_runs (eval_run_id, status, model, scenarios) VALUES ($1, 'queued', $2, $3)",
        eval_run_id, state.settings.read_model, len(scenarios or scenario_catalog()))
    job_id = await state.queue.enqueue("run_eval", {"eval_run_id": eval_run_id, "scenarios": scenarios},
                                       dedupe_key=f"run_eval:{eval_run_id}", max_attempts=1)
    return {"eval_run_id": eval_run_id, "job_id": job_id}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=chat.MAX_QUESTION)
    frame_id: str | None = None
    anomaly_id: str | None = None


async def _conversation(pool, principal: Principal, clip_id: str) -> str | None:
    """Whose conversation this is: None for the clip's owner or an admin, else the visitor's own on an example clip."""
    await require_clip(pool, principal, clip_id)
    return None if await can_edit_clip(pool, principal, clip_id) else principal.user_id or principal.session_id


def _message(m: dict) -> dict:
    """A visitor's chat run isn't openable (traces of an example clip's chats are its owner's), so no link to it."""
    return _jsonable({k: (None if k == "run_id" and m.get("visitor") else v) for k, v in m.items() if k != "visitor"})


@router.get("/clips/{clip_id}/chat")
async def get_chat(request: Request, clip_id: str, principal: Principal = Depends(current_principal)):
    state = _state(request)
    visitor = await _conversation(state.pool, principal, clip_id)
    return [_message(m) for m in await chat.history(state.pool, clip_id, visitor)]


@router.post("/clips/{clip_id}/chat")
async def ask(request: Request, clip_id: str, body: Question, principal: Principal = Depends(current_principal)):
    """Answers synchronously (a few seconds: the agent may call tools). The question is stored first."""
    state = _state(request)
    visitor = await _conversation(state.pool, principal, clip_id)
    if not principal.is_admin:
        key, is_guest = caller_key(principal, request)
        enforce(request, f"{key}:questions", state.settings.guest_questions_per_hour if is_guest
                else state.settings.user_questions_per_hour, "questions")
    ctx = SimpleNamespace(pool=state.pool, client=state.client, evidence=state.evidence)
    try:
        message = await chat.ask(ctx, clip_id, body.question, frame_id=body.frame_id, anomaly_id=body.anomaly_id,
                                 visitor=visitor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _message(message)


@router.delete("/clips/{clip_id}/chat", status_code=204)
async def clear_chat(request: Request, clip_id: str, principal: Principal = Depends(current_principal)):
    state = _state(request)
    await chat.clear(state.pool, clip_id, await _conversation(state.pool, principal, clip_id))
