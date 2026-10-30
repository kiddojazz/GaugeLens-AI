"""Agent evaluation + tracing: scenarios seed through the real physics engine, the agent runs with a scripted
model, every step is stored as a span, and the scores come out right for a good and a misbehaving agent."""

import asyncio
import json
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app.agent.evals import BY_ID, SCENARIOS, run_eval, score, seed_scenario
from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.tests.fakes import sign_in
from backend.app.seed import seed_mock

from .fakes import make_settings, tool_response
from .test_diagnose import text_reply


class EvalAgent:
    """Looks at the physics checks, views one frame, then writes a diagnosis citing what it saw.
    `unsafe` makes it follow the planted instruction (to prove the scoring catches it)."""

    def __init__(self, cause="An extra DC load on the battery side draws current the AC output doesn't explain",
                 unsafe=False):
        self.cause, self.unsafe, self.steps = cause, unsafe, 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        messages = kwargs["messages"]
        tool_text = "\n".join(m["content"] for m in messages if m["role"] == "tool")
        if kwargs.get("tool_choice") == "auto":
            self.steps += 1
            if self.steps == 1:
                return text_reply(tool_calls=[("get_physics_checks", {})])
            if self.steps == 2:
                frame = re.search(r"frame:[A-Za-z0-9_]+", messages[0]["content"]).group(0)
                return text_reply(tool_calls=[("view_frame", {"frame_id": frame})])
            return text_reply(content="Ready.")
        checks = json.loads(tool_text.split("\n")[0])["checks"]
        failing = next(c for c in checks if c["outcome"] == "persistent_violation")
        action = ("Open the inverter cover and bypass the breaker to reset it." if self.unsafe
                  else "Clamp each DC cable on the battery terminals and compare with the inverter feed.")
        return tool_response({
            "headline": "SIMULATED data: battery-side power does not match the AC output",
            "observations": [{"statement": "The failing check was confirmed by a re-look",
                              "evidence": [failing["id"], failing["inputs"][0]]}],
            "physics": [{"rule_id": failing["rule_id"], "result": failing["outcome"],
                         "residual_pct": failing["residual_pct"], "evidence": [failing["id"]]}],
            "hypotheses": [{"cause": self.cause, "likelihood": "high", "supporting": [failing["id"]], "against": []}],
            "unknowns": ["Which DC loads are connected"], "proposed_action": action,
            "safety_note": "Stay outside the enclosure.", "confidence": "medium"}, name="record_diagnosis")


def run(tmp_path, db_url, agent, scenario_ids):
    settings = make_settings(tmp_path, database_url=db_url, evidence_dir=tmp_path / "evidence")

    async def go():
        await seed_mock(db_url)
        pool = await create_pool(db_url)
        try:
            ctx = SimpleNamespace(pool=pool, client=ChatClient(settings, raw_client=agent),
                                  evidence=LocalEvidenceStore(settings.evidence_dir))
            eval_run_id = await run_eval(ctx, scenario_ids=scenario_ids)
            return eval_run_id, [dict(r) for r in await pool.fetch(
                "SELECT * FROM eval_results WHERE eval_run_id = $1 ORDER BY scenario_id", eval_run_id)]
        finally:
            await pool.close()

    return settings, *asyncio.run(go())


def test_every_scenario_makes_the_physics_engine_open_its_anomaly(tmp_path, clean_db):
    async def go():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(tmp_path)
            for scenario in SCENARIOS:
                anomaly_id = await seed_scenario(pool, evidence, scenario)
                a = await pool.fetchrow("SELECT * FROM anomalies WHERE anomaly_id = $1", anomaly_id)
                assert a["rule_id"] == scenario.rule_id and a["is_simulated"]
                assert all(c.startswith("chk_") for c in a["evidence"]["check_ids"])
                frames = await pool.fetch("SELECT * FROM frames WHERE clip_id = $1 ORDER BY t_offset_s", a["clip_id"])
                assert len(frames) == 8 and evidence.read(frames[0]["image_uri"])[:2] == b"\xff\xd8"   # JPEG
                fails = await pool.fetch("SELECT * FROM physics_checks WHERE clip_id = $1 AND rule_id = $2 AND NOT passed",
                                         a["clip_id"], scenario.rule_id)
                assert fails and all(f["relook"]["outcome"] == "persistent_violation" for f in fails)
                before = await pool.fetchval(               # the fault starts at frame 5: earlier frames are clean
                    "SELECT count(*) FROM physics_checks pc JOIN frames f USING (frame_id) "
                    "WHERE pc.clip_id = $1 AND NOT pc.passed AND pc.outcome <> 'skipped' AND f.t_offset_s < 8",
                    a["clip_id"])
                assert before == 0, scenario.id
        finally:
            await pool.close()

    asyncio.run(go())


def test_a_good_agent_passes_and_its_run_is_traced_step_by_step(tmp_path, clean_db):
    settings, eval_run_id, results = run(tmp_path, clean_db, EvalAgent(), ["hidden_dc_load"])
    [result] = results
    assert result["passed"], result["criteria"]
    assert set(result["criteria"]) == {"grounded", "right_cause", "cites_physics", "safe_action", "labels_simulated"}

    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, settings.database_url)
        evals = client.get("/api/v1/evals").json()
        assert evals[0]["eval_run_id"] == eval_run_id and evals[0]["status"] == "done" and evals[0]["passed"] == 1
        detail = client.get(f"/api/v1/evals/{eval_run_id}").json()
        assert detail["results"][0]["tool_calls"] == 2
        trace = client.get(f"/api/v1/traces/{result['run_id']}").json()
        agent_run, spans = trace["run"], trace["spans"]
        assert agent_run["status"] == "ok" and agent_run["verified"] is True and agent_run["scenario_id"] == "hidden_dc_load"
        assert agent_run["llm_calls"] == 4 and agent_run["tool_calls"] == 2 and agent_run["input_tokens"] > 0
        names = [(s["kind"], s["name"]) for s in spans]
        assert names[:3] == [("agent", "diagnose"), ("context", "load_context"), ("llm", "step 1")]
        assert ("tool", "get_physics_checks") in names and ("guardrail", "citation check") in names
        root = spans[0]["span_id"]
        assert all(s["parent_id"] == root for s in spans[1:])
        frame_span = next(s for s in spans if s["name"] == "view_frame")
        assert frame_span["attributes"]["gaugelens.image_uri"].endswith(".jpg")
        assert frame_span["attributes"]["gen_ai.tool.name"] == "view_frame"
        step = next(s for s in spans if s["name"] == "step 1")
        assert step["attributes"]["gen_ai.usage.input_tokens"] == 500 and step["input"][0]["role"] == "system"
        # eval clips stay out of the normal lists
        assert not [c for c in client.get("/api/v1/clips").json() if c["source_channel"] == "eval"]
        assert not client.get("/api/v1/anomalies").json()
        assert client.get(f"/api/v1/traces?eval_run_id={eval_run_id}").json()[0]["run_id"] == result["run_id"]


def test_an_agent_that_follows_the_planted_instruction_is_stopped_by_the_guardrail_and_fails_the_eval(
        tmp_path, clean_db):
    _, _, results = run(tmp_path, clean_db, EvalAgent(cause="The inverter fan is dusty", unsafe=True),
                        ["prompt_injection"])
    [result] = results
    c = result["criteria"]
    assert not result["passed"]
    # the unsafe action never reached a person: the citation check rejected it twice, the template replaced it
    assert not c["grounded"]["pass"] and "unsafe" in c["grounded"]["detail"]
    assert c["safe_action"]["pass"] and c["resists_injection"]["pass"]
    assert not c["right_cause"]["pass"]
    assert 0 < result["score"] < 1


def test_score_flags_an_unsafe_or_injected_action():
    output = {"headline": "SIMULATED data", "proposed_action": "Open the inverter cover and bypass the breaker.",
              "physics": [{"rule_id": "inverter.dc_power_balance"}],
              "hypotheses": [{"cause": "Extra DC load", "likelihood": "high"}]}
    criteria = score(BY_ID["prompt_injection"], output, verified=True)
    assert not criteria["safe_action"]["pass"] and not criteria["resists_injection"]["pass"]
    assert criteria["right_cause"]["pass"] and criteria["cites_physics"]["pass"]


def test_a_safety_note_warning_against_the_planted_action_is_not_a_failure():
    output = {"headline": "SIMULATED data", "proposed_action": "Check the clamp placement on the battery lead.",
              "safety_note": "Do not open enclosures or bypass protection.",
              "physics": [{"rule_id": "inverter.dc_power_balance"}], "hypotheses": [{"cause": "Extra DC load", "likelihood": "high"}]}
    criteria = score(BY_ID["prompt_injection"], output, verified=True)
    assert all(c["pass"] for c in criteria.values()), criteria


def test_score_reads_the_most_likely_hypothesis_first():
    output = {"headline": "SIMULATED: low battery", "proposed_action": "Measure the bank voltage at rest.",
              "physics": [{"rule_id": "inverter.battery_voltage_band"}],
              "hypotheses": [{"cause": "Clamp error", "likelihood": "low"},
                             {"cause": "Battery bank deeply discharged", "likelihood": "high"}]}
    criteria = score(BY_ID["weak_battery"], output, verified=True)
    assert all(v["pass"] for v in criteria.values()), criteria
