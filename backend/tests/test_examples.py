"""Example clips: anyone can read them, only the owner or an admin can change them, and each visitor's chat is theirs."""

import asyncio

from fastapi.testclient import TestClient

from backend.app import access
from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.app.purge import sweep
from backend.tests.fakes import TEST_PASSWORD, make_settings, sign_in
from backend.tests.test_chat import ChatAgent
from backend.tests.test_chat import setup as chat_setup


def guest(client) -> dict:
    return {access.HEADER: client.post("/api/v1/auth/guest").json()["token"]}


def query(db_url, sql, *args):
    async def go():
        pool = await create_pool(db_url)
        try:
            return await pool.fetch(sql, *args)
        finally:
            await pool.close()

    return asyncio.run(go())


def example(tmp_path, clean_db):
    agent = ChatAgent()
    settings, clip_id, _, anomaly_id = chat_setup(tmp_path, clean_db, agent)
    query(clean_db, "UPDATE clips SET source_channel = 'seed' WHERE clip_id = $1", clip_id)
    return settings, agent, clip_id, anomaly_id


def test_visitors_read_an_example_but_cant_change_it_and_chats_stay_private(tmp_path, clean_db):
    settings, agent, clip_id, anomaly_id = example(tmp_path, clean_db)
    client_impl = ChatClient(settings, raw_client=agent)
    with TestClient(create_app(settings=settings, client=client_impl, start_worker=False)) as client:
        alice, bob = guest(client), guest(client)
        assert client.get(f"/api/v1/clips/{clip_id}", headers=alice).status_code == 404     # private until published
        assert client.patch(f"/api/v1/clips/{clip_id}/public", json={"public": True}, headers=alice).status_code == 403

        with TestClient(create_app(settings=settings, start_worker=False)) as admin:   # its own app: one lifespan each
            sign_in(admin, clean_db)
            assert admin.patch(f"/api/v1/clips/{clip_id}/public", json={"public": True}).status_code == 200

            shown = client.get(f"/api/v1/clips/{clip_id}", headers=alice).json()
            assert shown["can_edit"] is False and shown["can_publish"] is False and shown["clip"]["is_public"] is True
            assert [c["clip_id"] for c in client.get("/api/v1/clips", headers=alice).json()] == [clip_id]
            assert client.get(f"/api/v1/anomalies/{anomaly_id}", headers=alice).json()["can_edit"] is False
            assert client.get(f"/api/v1/clips/{clip_id}/frames", headers=alice).status_code == 200

            assert client.post(f"/api/v1/anomalies/{anomaly_id}/diagnose", headers=alice).status_code == 404
            assert client.delete(f"/api/v1/clips/{clip_id}/chat", headers=alice).status_code == 204   # only their own

            asked = client.post(f"/api/v1/clips/{clip_id}/chat", json={"question": "Alice asks this?"}, headers=alice)
            assert asked.status_code == 200 and asked.json()["run_id"] is None
            assert [m["content"] for m in client.get(f"/api/v1/clips/{clip_id}/chat", headers=alice).json()][0] == "Alice asks this?"
            assert client.get(f"/api/v1/clips/{clip_id}/chat", headers=bob).json() == []
            assert admin.get(f"/api/v1/clips/{clip_id}/chat").json() == []

            run_id = query(clean_db, "SELECT run_id FROM chat_messages WHERE run_id IS NOT NULL")[0]["run_id"]
            assert client.get(f"/api/v1/traces/{run_id}", headers=bob).status_code == 404        # their question stays theirs
            listed = client.get(f"/api/v1/traces?anomaly_id={anomaly_id}", headers=bob).json()
            assert not [r for r in listed if r["kind"] == "chat"]

            assert client.post("/api/v1/auth/guest/end", content=alice[access.HEADER]).status_code == 204

        async def purge():
            pool = await create_pool(clean_db)
            try:
                await sweep(pool, LocalEvidenceStore(settings.evidence_dir), settings)
            finally:
                await pool.close()

        asyncio.run(purge())
        assert query(clean_db, "SELECT count(*) AS n FROM chat_messages")[0]["n"] == 0
        assert query(clean_db, "SELECT count(*) AS n FROM agent_runs WHERE kind = 'chat'")[0]["n"] == 0
        assert query(clean_db, "SELECT count(*) AS n FROM clips WHERE clip_id = $1", clip_id)[0]["n"] == 1   # the example stays


def test_signing_up_keeps_a_visitors_conversation(tmp_path, clean_db):
    settings, agent, clip_id, _ = example(tmp_path, clean_db)
    query(clean_db, "UPDATE clips SET is_public = true WHERE clip_id = $1", clip_id)
    app = create_app(settings=settings, client=ChatClient(settings, raw_client=agent), start_worker=False)
    with TestClient(app) as client:
        alice = guest(client)
        client.post(f"/api/v1/clips/{clip_id}/chat", json={"question": "Hello there?"}, headers=alice)
        client.post("/api/v1/auth/register", json={"email": "a@test.local", "password": TEST_PASSWORD}, headers=alice)
        assert client.get(f"/api/v1/clips/{clip_id}/chat").json()[0]["content"] == "Hello there?"


def test_only_your_own_or_unowned_clips_can_be_published(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence")
    video = ("v.mp4", b"video", "video/mp4")
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        guest_clip = client.post("/api/v1/clips", files={"file": video}, headers=guest(client)).json()["clip_id"]
        with TestClient(create_app(settings=settings, start_worker=False)) as member:
            sign_in(member, clean_db, email="m@test.local", role="user")
            private = member.post("/api/v1/clips", files={"file": video}).json()["clip_id"]
            assert member.patch(f"/api/v1/clips/{private}/public", json={"public": True}).status_code == 403
            with TestClient(create_app(settings=settings, start_worker=False)) as admin:
                sign_in(admin, clean_db)
                for other in (guest_clip, private):
                    assert admin.patch(f"/api/v1/clips/{other}/public", json={"public": True}).status_code == 400
                    assert admin.get(f"/api/v1/clips/{other}").json()["can_publish"] is False
                mine = admin.post("/api/v1/clips", files={"file": video}).json()["clip_id"]
                assert admin.get(f"/api/v1/clips/{mine}").json()["can_publish"] is True
                assert admin.patch(f"/api/v1/clips/{mine}/public", json={"public": True}).status_code == 200
                assert admin.patch(f"/api/v1/clips/{mine}/public", json={"public": False}).status_code == 200
