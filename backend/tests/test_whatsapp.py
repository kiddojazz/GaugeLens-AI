"""Messaging channels (WhatsApp, Messenger): webhook verification, signatures and parsing, then the shared
conversation (clip in, report, diagnosis with Approve / Reject, decisions, questions) against a fake client."""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.diagnose import run_diagnosis
from backend.app.agent.evals import BY_ID, seed_scenario
from backend.app.channels import conversation, messenger, whatsapp
from backend.app.channels.conversation import Inbound
from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.app.seed import seed_mock

from .fakes import make_settings
from .test_chat import ChatAgent
from .test_evals import EvalAgent

NUMBER = "2348012345678"
PSID = "7654321098765432"


class FakeClient:
    """Stands in for WhatsAppClient / MessengerClient: records what would be sent, serves a 'video'."""
    sent: list = []

    def __init__(self, settings):
        self.settings = settings

    async def send_text(self, to, body):
        FakeClient.sent.append(("text", to, body))
        return f"out.{len(FakeClient.sent)}"

    async def send_buttons(self, to, body, buttons):
        FakeClient.sent.append(("buttons", to, body, buttons))
        return f"out.{len(FakeClient.sent)}"

    async def mark_read(self, m):
        pass

    async def media(self, media):
        return b"\x00\x00\x00\x18ftypmp42 not a real video", "video/mp4"


@pytest.fixture
def fake(monkeypatch):
    FakeClient.sent = []
    monkeypatch.setattr(conversation, "client_for", lambda channel, settings: FakeClient(settings))
    return FakeClient


_n = iter(range(10**6))


def inbound(channel, sender, kind, **fields):
    return Inbound(channel=channel, id=f"{channel}.in{next(_n)}", sender=sender, kind=kind, **fields)


def run(db_url, settings, agent, steps):
    async def go():
        await seed_mock(db_url)
        pool = await create_pool(db_url)
        try:
            ctx = SimpleNamespace(pool=pool, settings=settings, client=ChatClient(settings, raw_client=agent),
                                  evidence=LocalEvidenceStore(settings.evidence_dir), queue=PostgresJobQueue(pool))
            return await steps(ctx)
        finally:
            await pool.close()
    return asyncio.run(go())


def job(payload):
    return SimpleNamespace(payload=payload)


async def ingest(ctx, m: Inbound):
    await conversation.ingest_job(job({"message": m.as_payload()}), ctx)


# ------------------------------------------------------------------------------------------- pure helpers --

def test_signature_and_whatsapp_parsing():
    body = b'{"x": 1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert whatsapp.signature_ok("s3cret", body, good)
    assert not whatsapp.signature_ok("s3cret", body, "sha256=00") and not whatsapp.signature_ok("s3cret", body, None)
    assert whatsapp.signature_ok(None, body, None)                                 # dev mode: no secret set
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": NUMBER, "profile": {"name": "Tayo"}}],
        "messages": [{"from": NUMBER, "id": "wamid.1", "type": "text", "text": {"body": "hi"}},
                     {"from": NUMBER, "id": "wamid.2", "type": "video", "video": {"id": "m1", "mime_type": "video/mp4"}},
                     {"from": NUMBER, "id": "wamid.3", "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": "approve:dg_1", "title": "Approve"}}}],
        "statuses": [{"id": "wamid.0", "status": "delivered"}]}}]}]}
    raw = whatsapp.inbound_messages(payload)
    assert raw[0]["profile_name"] == "Tayo"
    text, video, tap = (whatsapp.to_inbound(m) for m in raw)
    assert (text.kind, text.text) == ("text", "hi")
    assert video.kind == "video" and video.media == {"id": "m1", "mime_type": "video/mp4"}
    assert (tap.kind, tap.button_id) == ("button", "approve:dg_1")
    bare = payload["entry"][0]["changes"][0] | {"field": "messages"}           # the dashboard's sample shape
    assert [m["id"] for m in whatsapp.inbound_messages(bare)] == ["wamid.1", "wamid.2", "wamid.3"]


def test_messenger_parsing_skips_echoes_and_reads_videos_and_taps():
    payload = {"object": "page", "entry": [{"messaging": [
        {"sender": {"id": PSID}, "message": {"mid": "m.1", "text": "what did you find?"}},
        {"sender": {"id": PSID}, "message": {"mid": "m.2", "attachments": [
            {"type": "video", "payload": {"url": "https://cdn.example/v.mp4"}}]}},
        {"sender": {"id": PSID}, "postback": {"mid": "m.3", "payload": "reject:dg_9", "title": "Reject"}},
        {"sender": {"id": "PAGE"}, "message": {"mid": "m.4", "text": "our own reply", "is_echo": True}},
        {"sender": {"id": PSID}, "read": {"watermark": 1}}]}]}
    events = messenger.inbound_events(payload)
    assert [(e.kind, e.id) for e in events] == [("text", "m.1"), ("video", "m.2"), ("button", "m.3")]
    assert events[1].media == {"url": "https://cdn.example/v.mp4"} and events[2].button_id == "reject:dg_9"
    assert messenger.inbound_events({"object": "instagram", "entry": []}) == []
    assert messenger.to_plain("*Clip read.* 3 readings") == "Clip read. 3 readings"


def test_answers_lose_their_evidence_ids_in_messaging_apps():
    text = "It read **230** V [reading:rd_01ABC, frame:clip_1_f00004] — that is `normal` (check:chk_9)."
    assert conversation.plain(text) == "It read *230* V, that is normal ()."


# --------------------------------------------------------------------------------------------- webhooks --

def _jobs(db_url):
    async def go():
        pool = await create_pool(db_url)
        try:
            return await pool.fetch("SELECT kind, payload FROM jobs ORDER BY created_at")
        finally:
            await pool.close()
    return asyncio.run(go())


def test_both_webhooks_verify_check_signatures_and_take_each_message_once(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, webhook_verify_token="tok", meta_app_secret="s3cret")
    wa = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": NUMBER, "id": "wamid.A", "type": "text", "text": {"body": "hello"}}]}}]}]}).encode()
    fb = json.dumps({"object": "page", "entry": [{"messaging": [
        {"sender": {"id": PSID}, "message": {"mid": "m.A", "text": "hello"}}]}]}).encode()

    def sign(b):
        return {"X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", b, hashlib.sha256).hexdigest()}

    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        for path in ("/api/v1/whatsapp/webhook", "/api/v1/messenger/webhook"):
            ok = client.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "123"})
            assert ok.status_code == 200 and ok.text == "123"
            assert client.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "no",
                                            "hub.challenge": "1"}).status_code == 403
        assert client.post("/api/v1/whatsapp/webhook", content=wa, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
        assert client.post("/api/v1/whatsapp/webhook", content=wa, headers=sign(wa)).json() == {"received": 1}
        assert client.post("/api/v1/whatsapp/webhook", content=wa, headers=sign(wa)).json() == {"received": 0}
        assert client.post("/api/v1/messenger/webhook", content=fb, headers=sign(fb)).json() == {"received": 1}
        assert client.post("/api/v1/messenger/webhook", content=fb, headers=sign(fb)).json() == {"received": 0}
    jobs = _jobs(clean_db)
    assert [(j["kind"], j["payload"]["message"]["channel"]) for j in jobs] == [
        ("whatsapp_ingest", "whatsapp"), ("whatsapp_ingest", "messenger")]


def test_whatsapp_delivery_failures_are_recorded_with_metas_reason(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.X", "status": "failed", "recipient_id": NUMBER,
         "errors": [{"code": 131031, "title": "Business Account locked", "error_data": {"details": "locked"}}]}]}}]}]}
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        assert client.post("/api/v1/whatsapp/webhook", json=payload).json() == {"received": 0}

    async def stored():
        pool = await create_pool(clean_db)
        try:
            return await pool.fetchrow("SELECT * FROM channel_messages WHERE message_id = 'wamid.X:failed'")
        finally:
            await pool.close()
    row = asyncio.run(stored())
    assert row["type"] == "status" and row["body"]["errors"][0]["code"] == 131031


# ------------------------------------------------------------------------------------------ the journey --

@pytest.mark.parametrize("channel,sender", [("whatsapp", NUMBER), ("messenger", PSID)])
def test_clip_in_report_out_diagnosis_with_buttons_then_approve_and_ask(tmp_path, clean_db, fake, channel, sender):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence")

    async def journey(ctx):
        # 1. a video arrives: stored as a clip from this sender on this channel, process_clip queued, acknowledged
        await ingest(ctx, inbound(channel, sender, "video", media={"id": "m1"}))
        clip = await ctx.pool.fetchrow("SELECT * FROM clips WHERE reply_to = $1", sender)
        assert clip["source_channel"] == channel and clip["equipment_id"] == "inv_mock"
        assert await ctx.pool.fetchval("SELECT 1 FROM jobs WHERE dedupe_key = $1", f"process_clip:{clip['clip_id']}")
        assert "Got it" in fake.sent[-1][2]

        # 2. processing found a fault (a seeded scenario stands in for process_clip); the report goes out
        anomaly_id = await seed_scenario(ctx.pool, ctx.evidence, BY_ID["hidden_dc_load"])
        clip_id = await ctx.pool.fetchval("SELECT clip_id FROM anomalies WHERE anomaly_id = $1", anomaly_id)
        await ctx.pool.execute("UPDATE clips SET reply_to = $2, source_channel = $3 WHERE clip_id = $1",
                               clip_id, sender, channel)
        await conversation.notify_if_channel(ctx.pool, ctx.queue, clip_id, "clip_done")
        assert await ctx.pool.fetchval("SELECT 1 FROM jobs WHERE dedupe_key = $1", f"notify:clip_done:{clip_id}")
        await conversation.notify_job(job({"kind": "clip_done", "clip_id": clip_id}), ctx)
        report = fake.sent[-1][2]
        assert fake.sent[-1][1] == sender and "SIMULATED" in report and "1 possible fault" in report

        # 3. the diagnosis goes out with Approve / Reject buttons
        result = await run_diagnosis(ctx, anomaly_id)
        await conversation.notify_job(job({"kind": "diagnosis", "clip_id": clip_id, "anomaly_id": anomaly_id}), ctx)
        kind, to, body, buttons = fake.sent[-1]
        assert kind == "buttons" and to == sender and "Suggested next check" in body and "reading:" not in body
        assert buttons == [(f"approve:{result['diagnosis_id']}", "Approve"), (f"reject:{result['diagnosis_id']}", "Reject")]

        # 4. someone else can't decide it; the sender can, once
        await ingest(ctx, inbound(channel, "someone-else", "button", button_id=buttons[0][0]))
        assert "couldn't find" in fake.sent[-1][2]
        await ingest(ctx, inbound(channel, sender, "button", button_id=buttons[0][0]))
        d = await ctx.pool.fetchrow("SELECT decision, decided_by FROM diagnoses WHERE diagnosis_id = $1", result["diagnosis_id"])
        assert d["decision"] == "approved" and d["decided_by"] == f"{channel}:{sender}"
        assert fake.sent[-1][2].startswith("Approved.")
        await ingest(ctx, inbound(channel, sender, "button", button_id=buttons[0][0]))
        assert "already approved" in fake.sent[-1][2]

        # 5. a follow-up question goes to the chat agent; the answer arrives without evidence IDs
        ctx.client = ChatClient(settings, raw_client=ChatAgent())
        await ingest(ctx, inbound(channel, sender, "text", text="What was the battery current?"))
        answer = fake.sent[-1][2]
        assert "battery current" in answer and "reading:" not in answer
        stored = await ctx.pool.fetchrow("SELECT * FROM chat_messages WHERE role = 'assistant' ORDER BY created_at DESC LIMIT 1")
        assert stored["channel"] == channel and stored["clip_id"] == clip_id
        logged = await ctx.pool.fetch("SELECT DISTINCT channel FROM channel_messages")
        assert [r["channel"] for r in logged] == [channel]

    run(clean_db, settings, EvalAgent(), journey)


def test_greetings_images_and_bad_buttons_get_help(tmp_path, clean_db, fake):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence")

    async def steps(ctx):
        await ingest(ctx, inbound("messenger", PSID, "text", text="Get Started"))
        assert fake.sent[-1][2] == conversation.HELP
        await ingest(ctx, inbound("messenger", PSID, "image"))
        assert "send a 30-60 second clip" in fake.sent[-1][2]
        await ingest(ctx, inbound("messenger", PSID, "text", text="what did you find?"))
        assert fake.sent[-1][2] == conversation.HELP                      # no clip from this sender yet
        await ingest(ctx, inbound("messenger", PSID, "button", button_id="launch:x"))
        assert fake.sent[-1][2] == conversation.HELP
        assert await ctx.pool.fetchval("SELECT count(*) FROM channel_messages WHERE direction = 'out'") == 4

    run(clean_db, settings, ChatAgent(), steps)


def test_only_listed_senders_are_served_when_a_list_is_set(tmp_path, clean_db, fake):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path, channel_allowed_senders=("2340000000000",))

    async def steps(ctx):
        await ingest(ctx, inbound("whatsapp", NUMBER, "text", text="hi"))
        assert fake.sent == []

    run(clean_db, settings, ChatAgent(), steps)


def test_web_clips_never_trigger_a_channel_reply(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path)

    async def steps(ctx):
        anomaly_id = await seed_scenario(ctx.pool, ctx.evidence, BY_ID["weak_battery"])
        clip_id = await ctx.pool.fetchval("SELECT clip_id FROM anomalies WHERE anomaly_id = $1", anomaly_id)
        await conversation.notify_if_channel(ctx.pool, ctx.queue, clip_id, "clip_done")
        assert await ctx.pool.fetchval("SELECT count(*) FROM jobs WHERE kind = 'notify'") == 0

    run(clean_db, settings, ChatAgent(), steps)


def test_messenger_buttons_split_long_text_and_strip_bold():
    sent = []

    class Http:
        async def post(self, url, params=None, json=None):
            sent.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {"message_id": f"m.{len(sent)}"}, text="")

    client = messenger.MessengerClient(make_settings(Path("."), messenger_page_access_token="t"), http=Http())
    long_body = "*Headline*\n\n" + "detail " * 120 + "\n\nApprove the suggested check?"
    asyncio.run(client.send_buttons(PSID, long_body, [("approve:dg_1", "Approve"), ("reject:dg_1", "Reject")]))
    assert sent[0]["message"]["text"].startswith("Headline") and "*" not in sent[0]["message"]["text"]
    template = sent[1]["message"]["attachment"]["payload"]
    assert template["text"] == "Approve the suggested check?"
    assert [b["payload"] for b in template["buttons"]] == ["approve:dg_1", "reject:dg_1"]


def test_a_sim_tag_sent_as_its_own_message_marks_the_clip(tmp_path, clean_db, fake):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence")

    async def log_in(ctx, m):                               # what the webhook stores before queueing
        await conversation.log_message(ctx.pool, m.channel, "in", m.sender, m.kind, {"text": m.text}, message_id=m.id)
        await ingest(ctx, m)

    async def steps(ctx):
        await log_in(ctx, inbound("messenger", PSID, "text", text="#sim"))                 # before the video
        assert "video you send next" in fake.sent[-1][2]
        await ingest(ctx, inbound("messenger", PSID, "video", media={"url": "u"}))
        assert await ctx.pool.fetchval("SELECT is_demo_replay FROM clips WHERE reply_to = $1", PSID) is True

        await ingest(ctx, inbound("whatsapp", NUMBER, "video", media={"id": "m"}))         # after the video
        assert await ctx.pool.fetchval("SELECT is_demo_replay FROM clips WHERE reply_to = $1", NUMBER) is False
        await log_in(ctx, inbound("whatsapp", NUMBER, "text", text="#SIM"))
        assert "that clip is marked" in fake.sent[-1][2]
        assert await ctx.pool.fetchval("SELECT is_demo_replay FROM clips WHERE reply_to = $1", NUMBER) is True

    run(clean_db, settings, ChatAgent(), steps)
