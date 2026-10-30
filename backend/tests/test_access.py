"""Who sees what: guests see only their own session's clips and lose them when the session ends; accounts keep
theirs; admins see everything. Uploads aren't processed here (no worker), which is all access control needs."""

import asyncio

from fastapi.testclient import TestClient

from backend.app import access
from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.main import create_app
from backend.app.pipeline import identify
from backend.app.purge import sweep
from backend.tests.fakes import TEST_PASSWORD, make_settings, sign_in
from backend.tests.test_identify import COUNTER, LATRONIC, parse

VIDEO = ("scan.mp4", b"not really a video", "video/mp4")


def guest(client) -> dict:
    body = client.post("/api/v1/auth/guest").json()
    return {access.HEADER: body["token"]}


def upload(client, headers) -> str:
    response = client.post("/api/v1/clips", files={"file": VIDEO}, data={"equipment_id": "auto"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["clip_id"]


def run(db_url, steps):
    async def go():
        pool = await create_pool(db_url)
        try:
            return await steps(pool)
        finally:
            await pool.close()

    return asyncio.run(go())


def test_every_data_route_needs_a_session(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        for path in ("/clips", "/anomalies", "/corrections", "/equipment", "/auth/me", "/clips/x/chat",
                     "/evidence?uri=clip=x/source.mp4"):
            assert client.get(f"/api/v1{path}").status_code == 401, path
        assert client.post("/api/v1/clips", files={"file": VIDEO}).status_code == 401
        assert client.get("/api/v1/health").status_code == 200                 # open: the page's status dot


def test_a_guest_sees_only_their_own_clips_and_admins_see_all(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice, bob = guest(client), guest(client)
        clip = upload(client, alice)
        source = client.get(f"/api/v1/clips/{clip}", headers=alice).json()["clip"]["source_uri"]

        assert [c["clip_id"] for c in client.get("/api/v1/clips", headers=alice).json()] == [clip]
        assert client.get("/api/v1/clips", headers=bob).json() == []
        for path in (f"/clips/{clip}", f"/clips/{clip}/frames", f"/clips/{clip}/chat", f"/readings?clip_id={clip}"):
            assert client.get(f"/api/v1{path}", headers=bob).status_code == 404, path   # not 403: existence hidden
        assert client.get("/api/v1/evidence", params={"uri": source}, headers=bob).status_code == 404
        assert client.get("/api/v1/evidence", params={"uri": source, "s": alice[access.HEADER]}).status_code == 200
        assert client.get("/api/v1/traces", headers=alice).status_code == 403
        assert client.get("/api/v1/evals", headers=alice).status_code == 403
        assert client.get("/api/v1/admin/people", headers=alice).status_code == 403

        sign_in(client, clean_db)                                                       # the admin's cookie
        listed = client.get("/api/v1/clips").json()
        assert [(c["clip_id"], c["owner"]) for c in listed] == [(clip, "guest")]
        people = client.get("/api/v1/admin/people").json()
        assert any(g["clips"] == 1 for g in people["guests"])


def test_leaving_the_page_deletes_the_guests_clips_files_and_equipment(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice = guest(client)
        clip = upload(client, alice)
        session_id = client.get("/api/v1/auth/me", headers=alice).json()["session_id"]

        async def own_a_unit(pool):
            await identify.register(pool, parse(COUNTER), (None, session_id))
            await pool.execute("INSERT INTO chat_messages (message_id, clip_id, role, content) VALUES ('m1', $1, 'user', 'hi')", clip)

        run(clean_db, own_a_unit)
        assert (settings.evidence_dir / f"clip={clip}").is_dir()

        assert client.post("/api/v1/auth/guest/end", content=alice[access.HEADER]).status_code == 204   # the beacon
        assert client.get("/api/v1/clips", headers=alice).status_code == 401
        run(clean_db, lambda pool: sweep(pool, LocalEvidenceStore(settings.evidence_dir), settings))

    async def left(pool):
        return {t: await pool.fetchval(f"SELECT count(*) FROM {t}")
                for t in ("clips", "jobs", "clip_events", "chat_messages", "equipment", "displays", "instruments")}

    assert run(clean_db, left) == dict.fromkeys(("clips", "jobs", "clip_events", "chat_messages", "equipment",
                                                 "displays", "instruments"), 0)
    assert not (settings.evidence_dir / f"clip={clip}").exists()


def test_an_idle_guest_session_ends_and_is_purged(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, guest_idle_minutes=30)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice = guest(client)
        clip = upload(client, alice)
        run(clean_db, lambda pool: pool.execute("UPDATE sessions SET last_seen_at = now() - interval '31 minutes'"))
        assert client.get(f"/api/v1/clips/{clip}", headers=alice).status_code == 401
        run(clean_db, lambda pool: sweep(pool, LocalEvidenceStore(settings.evidence_dir), settings))
        assert run(clean_db, lambda pool: pool.fetchval("SELECT end_reason FROM sessions")) == "idle"
        assert run(clean_db, lambda pool: pool.fetchval("SELECT count(*) FROM clips")) == 0


def test_signing_up_keeps_the_guests_scans_and_they_survive_a_refresh(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    app = create_app(settings=settings, start_worker=False)
    with TestClient(app) as client:
        alice = guest(client)
        clip = upload(client, alice)
        made = client.post("/api/v1/auth/register", json={"email": "Alice@Example.com", "password": TEST_PASSWORD},
                           headers=alice)
        assert made.status_code == 200 and made.json()["kind"] == "user" and made.json()["clips_moved"] == 1
        assert made.json()["email"] == "alice@example.com"
        assert client.post("/api/v1/auth/register", json={"email": "alice@example.com", "password": TEST_PASSWORD}
                           ).status_code == 409
        run(clean_db, lambda pool: sweep(pool, LocalEvidenceStore(settings.evidence_dir), settings))
        cookie = client.cookies.get(access.COOKIE)

    with TestClient(app, cookies={access.COOKIE: cookie}) as refreshed:         # a new page load: no guest token
        assert refreshed.get("/api/v1/auth/me").json()["kind"] == "user"
        assert [c["clip_id"] for c in refreshed.get("/api/v1/clips").json()] == [clip]
        assert refreshed.post("/api/v1/auth/logout").status_code == 204
        refreshed.cookies.set(access.COOKIE, cookie)                             # the old cookie no longer works
        assert refreshed.get("/api/v1/clips").status_code == 401

    with TestClient(app) as again:
        assert again.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "nope-nope"}).status_code == 401
        assert again.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": TEST_PASSWORD}).status_code == 200
        assert [c["clip_id"] for c in again.get("/api/v1/clips").json()] == [clip]


def test_guests_have_an_upload_limit_and_cant_use_others_equipment(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, guest_max_clips=2)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice, bob = guest(client), guest(client)
        upload(client, alice)
        upload(client, alice)
        limited = client.post("/api/v1/clips", files={"file": VIDEO}, headers=alice)
        assert limited.status_code == 429 and "account" in limited.json()["detail"]
        assert client.get("/api/v1/auth/me", headers=alice).json()["clips_left"] == 0

        alice_session = client.get("/api/v1/auth/me", headers=alice).json()["session_id"]
        unit, _ = run(clean_db, lambda pool: identify.register(pool, parse(COUNTER), (None, alice_session)))
        refused = client.post("/api/v1/clips", files={"file": VIDEO}, data={"equipment_id": unit}, headers=bob)
        assert refused.status_code == 400
        assert unit not in [e["equipment_id"] for e in client.get("/api/v1/equipment", headers=bob).json()]
        assert client.patch(f"/api/v1/equipment/{unit}", json={"model": "x"}, headers=bob).status_code == 404


def test_identified_units_are_never_shared_between_people(clean_db):
    async def steps(pool):
        a, created_a = await identify.register(pool, parse(LATRONIC), ("usr_a", "ses_a"))
        again, created_again = await identify.register(pool, parse(LATRONIC), ("usr_a", "ses_a2"))
        b, created_b = await identify.register(pool, parse(LATRONIC), (None, "ses_b"))
        unowned, created_unowned = await identify.register(pool, parse(LATRONIC))
        return (a, created_a), (again, created_again), (b, created_b), (unowned, created_unowned)

    (a, ca), (again, cagain), (b, cb), (unowned, cc) = run(clean_db, steps)
    assert ca and again == a and not cagain                 # the same person's meter is recognised next time
    assert cb and b != a                                    # a guest's meter is their own
    assert cc and unowned not in (a, b)                     # an unowned clip never lands on a person's unit


def test_passwords_are_hashed_and_checked():
    stored = access.hash_password("s3cret-pass")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert access.check_password("s3cret-pass", stored)
    assert not access.check_password("s3cret-pasS", stored)
    assert not access.check_password("x", "garbage")
