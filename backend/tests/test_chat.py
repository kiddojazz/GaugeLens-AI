"""Chat about a clip: frame-aware questions, tool use, evidence-checked answers, history, traces, the API."""

import asyncio
import json
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app.agent import chat
from backend.app.agent.evals import BY_ID, seed_scenario
from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.tests.fakes import sign_in
from backend.app.seed import seed_mock

from .fakes import make_settings
from .test_diagnose import text_reply


class ChatAgent:
    """Step 1 reads the battery current; step 2 answers citing a reading it got back.
    `answer` overrides the final text (to test the evidence check and the unsafe screen)."""

    def __init__(self, answer=None):
        self.answer, self.calls = answer, []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append(list(messages))              # the agent keeps appending to the same list
        if kwargs.get("tools") and not any(m["role"] == "tool" for m in messages[-3:]):
            return text_reply(tool_calls=[("get_readings", {"role": "battery_i"})])
        if self.answer is not None:
            return text_reply(content=self.answer)
        tool = next(m["content"] for m in reversed(messages) if m["role"] == "tool")
        last = json.loads(tool)["readings"][-1]
        return text_reply(content=f"The battery current read {last['raw_text']} A at the end [{last['id']}].")


def setup(tmp_path, db_url, agent):
    settings = make_settings(tmp_path, database_url=db_url, evidence_dir=tmp_path / "evidence")

    async def go():
        await seed_mock(db_url)
        pool = await create_pool(db_url)
        try:
            anomaly_id = await seed_scenario(pool, LocalEvidenceStore(settings.evidence_dir), BY_ID["hidden_dc_load"])
            clip_id = await pool.fetchval("SELECT clip_id FROM anomalies WHERE anomaly_id = $1", anomaly_id)
            frame_id = await pool.fetchval("SELECT frame_id FROM frames WHERE clip_id = $1 ORDER BY t_offset_s DESC", clip_id)
            return clip_id, frame_id, anomaly_id
        finally:
            await pool.close()

    return settings, *asyncio.run(go())


def ask(settings, db_url, agent, clip_id, question, **kw):
    async def go():
        pool = await create_pool(db_url)
        try:
            ctx = SimpleNamespace(pool=pool, client=ChatClient(settings, raw_client=agent),
                                  evidence=LocalEvidenceStore(settings.evidence_dir))
            return await chat.ask(ctx, clip_id, question, **kw)
        finally:
            await pool.close()

    return asyncio.run(go())


def test_a_question_about_the_frame_in_view_gets_a_cited_checked_answer(tmp_path, clean_db):
    agent = ChatAgent()
    settings, clip_id, frame_id, anomaly_id = setup(tmp_path, clean_db, agent)
    answer = ask(settings, clean_db, agent, clip_id, "What is the battery current here?", frame_id=frame_id,
                 anomaly_id=anomaly_id)
    assert answer["role"] == "assistant" and answer["verified"] is True and answer["problems"] == []
    assert answer["citations"] and answer["citations"][0].startswith("reading:")
    first_user_turn = agent.calls[0][-1]["content"]
    assert f"frame:{frame_id}" in first_user_turn and "battery_i" in first_user_turn      # what was in view
    assert f"anomaly:{anomaly_id}" in agent.calls[0][0]["content"]                        # asked from the anomaly page

    follow_up = ask(settings, clean_db, agent, clip_id, "And before that?")
    assert follow_up["verified"] is True
    sent = [m["content"] for m in agent.calls[-1] if isinstance(m["content"], str)]
    assert "What is the battery current here?" in sent and answer["content"] in sent     # the conversation so far

    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, settings.database_url)
        history = client.get(f"/api/v1/clips/{clip_id}/chat").json()
        assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
        assert history[0]["frame_id"] == frame_id and history[0]["anomaly_id"] == anomaly_id
        trace = client.get(f"/api/v1/traces/{answer['run_id']}").json()
        assert trace["run"]["kind"] == "chat" and trace["run"]["clip_id"] == clip_id
        assert trace["run"]["headline"] == "What is the battery current here?"
        assert [s["kind"] for s in trace["spans"]][:3] == ["agent", "context", "llm"]
        assert any(s["kind"] == "guardrail" and s["status"] == "ok" for s in trace["spans"])
        assert client.delete(f"/api/v1/clips/{clip_id}/chat").status_code == 204
        assert client.get(f"/api/v1/clips/{clip_id}/chat").json() == []


def test_an_invented_id_or_number_is_flagged_after_one_retry(tmp_path, clean_db):
    agent = ChatAgent(answer="The current was 99.9 A [reading:rd_INVENTED].")
    settings, clip_id, _, _ = setup(tmp_path, clean_db, agent)
    answer = ask(settings, clean_db, agent, clip_id, "What was the peak current?")
    assert answer["verified"] is False
    assert "unknown evidence id reading:rd_INVENTED" in answer["problems"]
    assert "number 99.9 not in any tool result" in answer["problems"]
    assert "failed the evidence check" in agent.calls[-1][-1]["content"]                 # the retry was asked for


def test_unsafe_advice_is_replaced_by_a_safe_refusal(tmp_path, clean_db):
    agent = ChatAgent(answer="Open the inverter cover and bypass the breaker to reset it.")
    settings, clip_id, _, _ = setup(tmp_path, clean_db, agent)
    answer = ask(settings, clean_db, agent, clip_id, "How do I reset it?")
    assert answer["content"] == chat.REFUSAL and answer["verified"] is False


def test_answers_are_stored_without_em_dashes(tmp_path, clean_db):
    agent = ChatAgent(answer="I can't tell — the evidence doesn't show it.")
    settings, clip_id, _, _ = setup(tmp_path, clean_db, agent)
    answer = ask(settings, clean_db, agent, clip_id, "Which is wrong?")
    assert answer["content"] == "I can't tell, the evidence doesn't show it." and answer["verified"] is True


def test_counting_numbers_are_not_mistaken_for_readings():
    assert chat.answer_problems("I checked 3 readings on frame 2.", "") == []
    assert chat.answer_problems("It read 25.2 V.", "battery_v 25.2") == []
    assert chat.answer_problems("It read 31.7 A.", "battery_i 28.0") == ["number 31.7 not in any tool result"]


def test_the_api_rejects_unknown_clips_and_empty_questions(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, settings.database_url)
        assert client.post("/api/v1/clips/clip_nope/chat", json={"question": "hi"}).status_code == 404
        assert client.post("/api/v1/clips/clip_nope/chat", json={"question": ""}).status_code == 422


def test_a_number_the_user_typed_doesnt_count_as_evidence(tmp_path, clean_db):
    agent = ChatAgent()
    settings, clip_id, _, _ = setup(tmp_path, clean_db, agent)
    ask(settings, clean_db, agent, clip_id, "Isn't the current 987.6 A?")
    agent.answer = "Yes, the current is 987.6 A."                   # repeats it as fact on the next turn
    answer = ask(settings, clean_db, agent, clip_id, "So what is it?")
    assert answer["verified"] is False and "number 987.6 not in any tool result" in answer["problems"]
