"""End to end: confirmed physics violation -> anomaly -> diagnose job -> agent with real tools on the DB
(scripted model) -> citation check -> stored diagnosis -> human decision through the API."""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.events import fetch_after
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.app.pipeline.clips import create_clip
from backend.app.pipeline.read_digital import TOOL_NAME
from backend.app.seed import seed_mock
from backend.tests.fakes import make_settings, sign_in, tool_response
from backend.tests.test_process_clip import CLAMP, OUTPUT_PAGE_MISREAD, setup


def text_reply(content=None, tool_calls=None):
    calls = [SimpleNamespace(id=f"tc_{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
             for i, (n, a) in enumerate(tool_calls or [])]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls or None))],
                           usage=SimpleNamespace(prompt_tokens=500, completion_tokens=50))


class ScriptedAgent:
    """Reads: the 838 W misread (re-look confirms it). Agent: get_physics_checks -> view_frame -> ready.
    Compose: a diagnosis citing IDs from the tool results (or, if `cite_fake`, an invented ID)."""

    def __init__(self, cite_fake=False):
        self.cite_fake, self.steps, self.composes, self.saw_image = cite_fake, 0, 0, False
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        messages = kwargs["messages"]
        system = messages[0]["content"]
        if not system.startswith("You are GaugeLens's diagnosis assistant"):
            text = messages[1]["content"][0]["text"]
            return tool_response(CLAMP if "clamp" in text else OUTPUT_PAGE_MISREAD, name=TOOL_NAME)
        tool_text = "\n".join(m["content"] for m in messages if m["role"] == "tool")
        if kwargs.get("tool_choice") == "auto":                          # an agent step
            self.saw_image |= any(m["role"] == "user" and isinstance(m["content"], list) for m in messages)
            self.steps += 1
            if self.steps == 1:
                return text_reply(tool_calls=[("get_physics_checks", {"rule_id": "inverter.power_factor_bound"})])
            if self.steps == 2:
                frame = re.search(r"frame:[A-Za-z0-9_]+", system).group(0)
                return text_reply(tool_calls=[("view_frame", {"frame_id": frame})])
            return text_reply(content="I have enough evidence and am ready to write the diagnosis.")
        self.composes += 1                                                # the structured diagnosis
        check = json.loads(tool_text.split("\n")[0])["checks"][0]
        reading = re.search(r"reading:rd_[A-Za-z0-9]+", tool_text).group(0)
        evidence = ["reading:rd_INVENTED"] if self.cite_fake else [reading, check["id"]]
        return tool_response({
            "headline": "Displayed output power exceeds apparent power",
            "observations": [{"statement": "Output power was displayed as 838 W against 393 VA", "evidence": evidence}],
            "physics": [{"rule_id": check["rule_id"], "result": check["outcome"], "residual_pct": None,
                         "evidence": [check["id"]]}],
            "hypotheses": [{"cause": "The W and VA fields are mislabelled on this display", "likelihood": "medium",
                            "supporting": [check["id"]], "against": []},
                           {"cause": "Inverter metering fault", "likelihood": "low", "supporting": [], "against": []}],
            "unknowns": ["No AC current is displayed"],
            "proposed_action": "Measure the AC output current with a clamp meter under the same load.",
            "safety_note": "Do not open the inverter enclosure.", "confidence": "medium"}, name="record_diagnosis")


def run(tmp_path, db_url, agent):
    settings, _, video = setup(tmp_path, db_url)

    async def scenario():
        await seed_mock(db_url)
        pool = await create_pool(db_url)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            queue = PostgresJobQueue(pool)
            clip_id, _ = await create_clip(pool, evidence, queue, video=video.read_bytes(), filename="clip.mp4",
                                           equipment_id="inv_mock", is_demo_replay=True)
            worker = Worker(pool, settings, client=ChatClient(settings, raw_client=agent), evidence=evidence)
            await worker.run_once()                                       # process_clip -> anomaly + diagnose job
            anomaly = dict(await pool.fetchrow("SELECT * FROM anomalies WHERE clip_id = $1", clip_id))
            job = await pool.fetchrow("SELECT job_id, status FROM jobs WHERE kind = 'diagnose'")
            await worker.run_once()                                       # the diagnose job
            diagnosis = dict(await pool.fetchrow("SELECT * FROM diagnoses WHERE anomaly_id = $1", anomaly["anomaly_id"]))
            events = await fetch_after(pool, clip_id, 0, limit=2000)
            return anomaly, dict(job), diagnosis, events, clip_id
        finally:
            await pool.close()

    return settings, asyncio.run(scenario())


def test_confirmed_violation_is_diagnosed_with_cited_evidence_and_decided_by_a_human(tmp_path, clean_db):
    agent = ScriptedAgent()
    settings, (anomaly, job, diagnosis, events, clip_id) = run(tmp_path, clean_db, agent)
    assert anomaly["kind"] == "physics" and anomaly["rule_id"] == "inverter.power_factor_bound"
    assert anomaly["is_simulated"] is True and anomaly["evidence"]["check_ids"]
    assert job["status"] == "queued"                                      # queued by process_clip
    assert diagnosis["verified"] is True and diagnosis["decision"] == "pending"
    output = diagnosis["output"]
    assert output["observations"][0]["evidence"][1].startswith("check:")
    assert [t["tool"] for t in output["trace"]] == ["get_physics_checks", "view_frame"]
    assert agent.saw_image                                                # the frame image reached the model
    types = [e["type"] for e in events]
    assert "anomaly" in types and "diagnosis" in types

    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, settings.database_url)
        listed = client.get(f"/api/v1/anomalies?clip_id={clip_id}").json()
        assert listed[0]["diagnosis_id"] == diagnosis["diagnosis_id"] and listed[0]["decision"] == "pending"
        assert listed[0]["headline"] == output["headline"]
        decided = client.post(f"/api/v1/diagnoses/{diagnosis['diagnosis_id']}/decision",
                              json={"decision": "approved", "decided_by": "test-technician"})
        assert decided.status_code == 200 and decided.json()["decision"] == "approved"
        again = client.post(f"/api/v1/diagnoses/{diagnosis['diagnosis_id']}/decision", json={"decision": "rejected"})
        assert again.status_code == 409                                   # decisions are final


def test_an_unverifiable_diagnosis_falls_back_to_the_honest_template(tmp_path, clean_db):
    agent = ScriptedAgent(cite_fake=True)
    _, (anomaly, _, diagnosis, _, _) = run(tmp_path, clean_db, agent)
    assert agent.composes == 2                                            # one retry with the problems listed
    assert diagnosis["verified"] is False and diagnosis["model"].startswith("template")
    output = diagnosis["output"]
    assert output["headline"].startswith("SIMULATED: ") and output["confidence"] == "low"
    assert "unknown evidence id reading:rd_INVENTED" in output["citation_problems"]


class DownAgent:
    """The model endpoint is out."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        raise ConnectionError("model endpoint unreachable")


def test_a_model_outage_retries_then_stores_the_honest_template(tmp_path, clean_db):
    from backend.app.agent.diagnose import diagnose_job
    from backend.app.agent.evals import BY_ID, seed_scenario
    from backend.app.llm import ModelUnavailable

    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence", max_retries=0)

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            anomaly_id = await seed_scenario(pool, evidence, BY_ID["hidden_dc_load"])
            ctx = SimpleNamespace(pool=pool, client=ChatClient(settings, raw_client=DownAgent()), evidence=evidence,
                                  settings=settings, queue=PostgresJobQueue(pool))
            job = SimpleNamespace(payload={"anomaly_id": anomaly_id}, attempts=1, max_attempts=3)
            with pytest.raises(ModelUnavailable):                      # not the last attempt: retried, nothing stored
                await diagnose_job(job, ctx)
            assert await pool.fetchval("SELECT count(*) FROM diagnoses") == 0

            job.attempts = 3
            await diagnose_job(job, ctx)                               # the last attempt never leaves it empty
            stored = await pool.fetchrow("SELECT verified, model FROM diagnoses WHERE anomaly_id = $1", anomaly_id)
            assert stored["verified"] is False and stored["model"].startswith("template")
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_a_failed_tool_tells_the_agent_instead_of_ending_the_run():
    from backend.app.agent.tools import AgentTools

    tools = AgentTools(None, None, "clip", "equipment")

    async def broken(role):
        raise KeyError("boom")

    tools.get_readings = broken
    result = asyncio.run(tools.run("get_readings", json.dumps({"role": "x"})))
    assert result.text.startswith("TOOL ERROR: get_readings failed (KeyError)")

