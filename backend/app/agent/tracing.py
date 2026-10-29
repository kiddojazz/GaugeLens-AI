"""Agent run tracing: every step of a run becomes a span, stored in agent_runs / agent_spans.

The shape follows OpenTelemetry (trace = run, spans with parent ids, start/end, status, attributes) and the
GenAI semantic conventions for attribute names (gen_ai.request.model, gen_ai.usage.input_tokens,
gen_ai.tool.name ...), so an OTLP exporter can ship the same spans to Langfuse or any OTEL backend later.
Spans are buffered in memory and written when the run finishes (one transaction, no per-step writes).

    tracer = await RunTracer.start(pool, anomaly_id=..., model=..., prompt_version=...)
    with tracer.span("get_readings", "tool", input=args) as span:
        span.output = ...
    await tracer.finish(diagnosis_id=..., verified=True)
"""

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..ids import new_id

MAX_TEXT = 20_000             # per string stored in a span; full prompts stay in the model cache


def _clip_text(value: str) -> str:
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + f"... [{len(value) - MAX_TEXT} more characters]"


def sanitize_messages(messages: list) -> list:
    """Chat messages as stored in a span: text kept (capped), images replaced by a marker."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = [p.get("text", "") if p.get("type") == "text" else "[image]" for p in content]
            content = " ".join(content)
        entry = {"role": m.get("role"), "content": _clip_text(content or "")}
        if m.get("tool_calls"):
            entry["tool_calls"] = [{"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                                   for c in m["tool_calls"]]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


def as_json(text: str):
    """Tool results are JSON text more often than not; store them structured so the UI can render them."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return _clip_text(text or "")


@dataclass
class Span:
    span_id: str
    parent_id: str | None
    seq: int
    name: str
    kind: str
    started_at: datetime
    t0: float
    attributes: dict = field(default_factory=dict)
    input: object = None
    output: object = None
    status: str = "ok"
    duration_ms: int | None = None

    def fail(self, message: str) -> None:
        self.status = "error"
        self.attributes["error.message"] = message

    def record_call(self, meta) -> None:
        """Model-call attributes (llm.client.CallMeta) in GenAI semantic-convention names."""
        self.attributes.update({
            "gen_ai.system": meta.provider, "gen_ai.request.model": meta.model,
            "gen_ai.usage.input_tokens": meta.input_tokens, "gen_ai.usage.output_tokens": meta.output_tokens,
            "gaugelens.cache_hit": meta.cache_hit, "gaugelens.purpose": meta.purpose,
            "gaugelens.prompt_version": meta.prompt_version, "gaugelens.model_call_id": meta.call_id})


class RunTracer:
    def __init__(self, pool, run_id: str, *, model: str | None, prompt_version: str | None):
        self.pool, self.run_id = pool, run_id
        self.model, self.prompt_version = model, prompt_version
        self.spans: list[Span] = []
        self._stack: list[Span] = []
        self._t0 = time.perf_counter()
        self._started = datetime.now(timezone.utc)

    @classmethod
    async def start(cls, pool, *, anomaly_id: str | None = None, kind: str = "diagnosis", model: str | None = None,
                    prompt_version: str | None = None, eval_run_id: str | None = None,
                    scenario_id: str | None = None, clip_id: str | None = None,
                    title: str | None = None) -> "RunTracer":
        tracer = cls(pool, new_id("run_"), model=model, prompt_version=prompt_version)
        await pool.execute(
            """INSERT INTO agent_runs (run_id, kind, anomaly_id, eval_run_id, scenario_id, model, prompt_version, started_at,
                                       clip_id, title)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            tracer.run_id, kind, anomaly_id, eval_run_id, scenario_id, model, prompt_version, tracer._started,
            clip_id, title)
        return tracer

    @contextmanager
    def span(self, name: str, kind: str, *, input=None, **attributes):
        now = time.perf_counter()
        span = Span(new_id("sp_"), self._stack[-1].span_id if self._stack else None, len(self.spans), name, kind,
                    self._started + timedelta(seconds=now - self._t0), now, dict(attributes), input)
        self.spans.append(span)
        self._stack.append(span)
        try:
            yield span
        except BaseException as exc:
            span.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.duration_ms = int((time.perf_counter() - span.t0) * 1000)
            self._stack.pop()

    async def finish(self, *, diagnosis_id: str | None = None, verified: bool | None = None,
                     error: str | None = None) -> None:
        llm = [s for s in self.spans if s.kind == "llm"]
        tools = [s for s in self.spans if s.kind == "tool" and s.status == "ok"]
        tokens_in = sum(s.attributes.get("gen_ai.usage.input_tokens") or 0 for s in llm)
        tokens_out = sum(s.attributes.get("gen_ai.usage.output_tokens") or 0 for s in llm)
        duration = int((time.perf_counter() - self._t0) * 1000)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """INSERT INTO agent_spans (span_id, run_id, parent_id, seq, name, kind, status, started_at, ended_at,
                                            duration_ms, attributes, input, output)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
                [(s.span_id, self.run_id, s.parent_id, s.seq, s.name, s.kind, s.status, s.started_at,
                  s.started_at + timedelta(milliseconds=s.duration_ms or 0), s.duration_ms,
                  json.loads(json.dumps(s.attributes, default=str)),
                  json.loads(json.dumps(s.input, default=str)) if s.input is not None else None,
                  json.loads(json.dumps(s.output, default=str)) if s.output is not None else None)
                 for s in self.spans])
            await conn.execute(
                """UPDATE agent_runs SET status = $2, diagnosis_id = $3, verified = $4, ended_at = $5, duration_ms = $6,
                       llm_calls = $7, tool_calls = $8, input_tokens = $9, output_tokens = $10, error = $11
                   WHERE run_id = $1""",
                self.run_id, "error" if error else "ok", diagnosis_id, verified,
                self._started + timedelta(milliseconds=duration), duration, len(llm), len(tools), tokens_in,
                tokens_out, error)
