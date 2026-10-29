"""The agent's read-only tools. Fixed, parameterised queries (the model never writes SQL), results capped,
errors returned as text (GridIQ style), and every result carries the IDs the agent may cite.
view_frame returns text; its image is attached by the loop as a user message (images are only accepted in
user messages)."""

import json
from dataclasses import dataclass

MAX_ROWS = 120
MAX_FRAME_VIEWS = 3

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_readings",
        "description": "Readings of one physics role in this clip over time (value, raw text, status, reading id).",
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "description": "Physics role, e.g. battery_v, battery_i, output_w."},
            "from_t": {"type": "number", "description": "Start, seconds into the clip (optional)."},
            "to_t": {"type": "number", "description": "End, seconds into the clip (optional)."}},
            "required": ["role"]}}},
    {"type": "function", "function": {
        "name": "get_physics_checks",
        "description": "Physics check results in this clip: rule, outcome, expected, observed, residual, check id.",
        "parameters": {"type": "object", "properties": {
            "rule_id": {"type": "string", "description": "Only this rule (optional)."},
            "from_t": {"type": "number"}, "to_t": {"type": "number"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_relook_attempts",
        "description": "The re-look attempts behind a check: what was re-read, how, and the outcome.",
        "parameters": {"type": "object", "properties": {"check_id": {"type": "string"}}, "required": ["check_id"]}}},
    {"type": "function", "function": {
        "name": "view_frame",
        "description": f"Look at one video frame (the image is attached after this result). At most {MAX_FRAME_VIEWS}.",
        "parameters": {"type": "object", "properties": {"frame_id": {"type": "string"}}, "required": ["frame_id"]}}},
    {"type": "function", "function": {
        "name": "search_manual",
        "description": "Search the equipment manual for a symptom or topic.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]


@dataclass
class ToolResult:
    text: str
    image: bytes | None = None
    frame_id: str | None = None
    image_uri: str | None = None          # evidence URI of the frame, for the trace view


def _strip(prefix: str, value: str) -> str:
    return value.split(":", 1)[1] if value.startswith(prefix + ":") else value


class AgentTools:
    def __init__(self, pool, evidence, clip_id: str, equipment_id: str):
        self.pool, self.evidence, self.clip_id, self.equipment_id = pool, evidence, clip_id, equipment_id
        self.frame_views = 0

    async def run(self, name: str, arguments: str) -> ToolResult:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return ToolResult(f"TOOL ERROR: arguments are not valid JSON: {arguments[:200]}")
        handler = {"get_readings": self.get_readings, "get_physics_checks": self.get_physics_checks,
                   "get_relook_attempts": self.get_relook_attempts, "view_frame": self.view_frame,
                   "search_manual": self.search_manual}.get(name)
        if handler is None:
            return ToolResult(f"TOOL ERROR: unknown tool {name}")
        try:
            return await handler(**args)
        except TypeError as exc:
            return ToolResult(f"TOOL ERROR: bad arguments for {name}: {exc}")

    async def get_readings(self, role: str, from_t: float | None = None, to_t: float | None = None) -> ToolResult:
        rows = await self.pool.fetch(
            """SELECT r.reading_id, f.frame_id, f.t_offset_s AS t, r.value, r.unit, r.raw_text, r.status, r.attempt,
                      r.confidence, r.reused_from IS NOT NULL AS reused
               FROM readings r JOIN frames f USING (frame_id) JOIN instruments i USING (instrument_id)
               WHERE f.clip_id = $1 AND i.physics_role = $2
                 AND ($3::float8 IS NULL OR f.t_offset_s >= $3) AND ($4::float8 IS NULL OR f.t_offset_s <= $4)
               ORDER BY f.t_offset_s, r.attempt""", self.clip_id, role, from_t, to_t)
        if not rows:
            return ToolResult(f"No readings of '{role}' in this clip for that time range. (No rows is not zero.)")
        fresh = [r for r in rows if not r["reused"]]                    # reused copies repeat the same value
        shown = fresh[:MAX_ROWS]
        payload = {"role": role, "readings": [
            {"id": f"reading:{r['reading_id']}", "frame": f"frame:{r['frame_id']}", "t": float(r["t"]),
             "value": r["value"], "unit": r["unit"], "raw_text": r["raw_text"], "status": r["status"],
             "attempt": r["attempt"], "confidence": r["confidence"]} for r in shown],
            "note": f"{len(rows) - len(fresh)} more readings are unchanged copies of these (same crop)."}
        if len(fresh) > MAX_ROWS:
            payload["warning"] = f"Only the first {MAX_ROWS} of {len(fresh)} fresh readings are shown; narrow the time range."
        return ToolResult(json.dumps(payload, default=str))

    async def get_physics_checks(self, rule_id: str | None = None, from_t: float | None = None,
                                 to_t: float | None = None) -> ToolResult:
        rows = await self.pool.fetch(
            """SELECT pc.check_id, pc.rule_id, pc.outcome, pc.expected, pc.observed, pc.residual_pct, pc.tolerance_used,
                      pc.reason, pc.reading_ids, pc.suspect_reading_id, f.t_offset_s AS t
               FROM physics_checks pc JOIN frames f USING (frame_id)
               WHERE pc.clip_id = $1 AND ($2::text IS NULL OR pc.rule_id = $2)
                 AND ($3::float8 IS NULL OR f.t_offset_s >= $3) AND ($4::float8 IS NULL OR f.t_offset_s <= $4)
                 AND pc.outcome <> 'skipped'
               ORDER BY pc.ts LIMIT $5""", self.clip_id, rule_id, from_t, to_t, MAX_ROWS)
        skipped = await self.pool.fetchval(
            "SELECT count(*) FROM physics_checks WHERE clip_id = $1 AND outcome = 'skipped' AND ($2::text IS NULL OR rule_id = $2)",
            self.clip_id, rule_id)
        if not rows:
            return ToolResult(f"No evaluated physics checks match. ({skipped} checks were skipped for missing/stale inputs.)")
        return ToolResult(json.dumps({"checks": [
            {"id": f"check:{r['check_id']}", "rule_id": r["rule_id"], "outcome": r["outcome"], "t": float(r["t"]),
             "expected": r["expected"], "observed": r["observed"], "residual_pct": r["residual_pct"],
             "tolerance_pct": r["tolerance_used"], "inputs": [f"reading:{i}" for i in r["reading_ids"]],
             "suspect": f"reading:{r['suspect_reading_id']}" if r["suspect_reading_id"] else None}
            for r in rows], "skipped_checks": skipped}, default=str))

    async def get_relook_attempts(self, check_id: str) -> ToolResult:
        row = await self.pool.fetchrow("SELECT relook, rule_id FROM physics_checks WHERE check_id = $1 AND clip_id = $2",
                                       _strip("check", check_id), self.clip_id)
        if row is None:
            return ToolResult(f"TOOL ERROR: no check {check_id} in this clip.")
        if not row["relook"]:
            return ToolResult(f"check:{_strip('check', check_id)} had no re-look.")
        relook = row["relook"]
        attempts = [{"attempt": a["attempt"], "strategy": a["strategy"], "read": a["raw_text"], "unit": a.get("unit"),
                     "outcome": a.get("outcome"), "id": f"reading:{a['reading_id']}"} for a in relook["attempts"]]
        return ToolResult(json.dumps({"check": f"check:{_strip('check', check_id)}", "rule_id": row["rule_id"],
                                      "outcome": relook["outcome"], "suspect": f"reading:{relook['suspect_reading_id']}",
                                      "attempts": attempts}))

    async def view_frame(self, frame_id: str) -> ToolResult:
        if self.frame_views >= MAX_FRAME_VIEWS:
            return ToolResult(f"TOOL ERROR: frame view budget ({MAX_FRAME_VIEWS}) used up.")
        frame_id = _strip("frame", frame_id)
        row = await self.pool.fetchrow("SELECT frame_id, t_offset_s, image_uri FROM frames WHERE frame_id = $1 AND clip_id = $2",
                                       frame_id, self.clip_id)
        if row is None:
            return ToolResult(f"TOOL ERROR: no frame {frame_id} in this clip.")
        self.frame_views += 1
        readings = await self.pool.fetch(
            """SELECT r.reading_id, i.physics_role, r.raw_text, r.unit, r.status FROM readings r
               JOIN instruments i USING (instrument_id) WHERE r.frame_id = $1 ORDER BY i.physics_role, r.attempt""", frame_id)
        text = json.dumps({"frame": f"frame:{frame_id}", "t": float(row["t_offset_s"]), "readings_on_this_frame": [
            {"id": f"reading:{r['reading_id']}", "role": r["physics_role"], "raw_text": r["raw_text"], "unit": r["unit"],
             "status": r["status"]} for r in readings], "image": "attached in the next message"})
        return ToolResult(text, image=self.evidence.read(row["image_uri"]), frame_id=frame_id,
                          image_uri=row["image_uri"])

    async def search_manual(self, query: str) -> ToolResult:
        doc = await self.pool.fetchval("SELECT manual_doc_id FROM equipment WHERE equipment_id = $1", self.equipment_id)
        if not doc:
            return ToolResult("No manual is registered for this equipment. Do not cite a manual.")
        return ToolResult(f"Manual {doc} is registered but manual search is not available yet.")
