"""Protection for a public link: credit-spending calls are limited per person, and a new guest session doesn't reset it."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app import access
from backend.app.limits import RateLimiter, client_ip
from backend.app.main import create_app
from backend.tests.fakes import make_settings, sign_in

VIDEO = ("scan.mp4", b"not really a video", "video/mp4")


def guest(client, ip="41.58.0.7") -> dict:
    response = client.post("/api/v1/auth/guest", headers={"X-Forwarded-For": ip})
    assert response.status_code == 200, response.text
    return {access.HEADER: response.json()["token"], "X-Forwarded-For": ip}


def request_from(forwarded: str):
    return SimpleNamespace(headers={"x-forwarded-for": forwarded} if forwarded else {},
                           client=SimpleNamespace(host="10.0.0.9"))


def test_a_forged_forwarded_header_doesnt_change_who_you_are():
    assert client_ip(request_from("8.8.8.8, 1.1.1.1")) == "1.1.1.1"          # the client wrote 8.8.8.8, the proxy 1.1.1.1
    assert client_ip(request_from("8.8.4.4, 1.1.1.1, 100.100.0.67, 10.0.0.2")) == "1.1.1.1"    # internal hops skipped
    assert client_ip(request_from("not-an-ip")) == "10.0.0.9"
    assert client_ip(request_from("")) == "10.0.0.9"


def test_the_window_slides():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert limiter.retry_after("k", 2) == 0 and limiter.retry_after("k", 2) == 0
    assert limiter.retry_after("k", 2) == 3600
    assert limiter.retry_after("other", 2) == 0                               # keys are independent
    now[0] = 3599.0
    assert limiter.retry_after("k", 2) == 1
    now[0] = 3600.0
    assert limiter.retry_after("k", 2) == 0


def test_new_guest_sessions_dont_reset_the_upload_limit(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, guest_uploads_per_hour=2)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        first = guest(client)
        for _ in range(2):
            assert client.post("/api/v1/clips", files={"file": VIDEO}, headers=first).status_code == 201
        second = guest(client)                                               # refresh, or a deliberate new session
        limited = client.post("/api/v1/clips", files={"file": VIDEO}, headers=second)
        assert limited.status_code == 429 and "try again in" in limited.json()["detail"]
        assert int(limited.headers["retry-after"]) > 0
        other_person = guest(client, "102.89.0.4")
        assert client.post("/api/v1/clips", files={"file": VIDEO}, headers=other_person).status_code == 201


def test_questions_are_limited_before_any_model_call(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, guest_questions_per_hour=1)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice = guest(client)
        clip = client.post("/api/v1/clips", files={"file": VIDEO}, headers=alice).json()["clip_id"]
        limits = client.app.state.limiter
        limits.hits[f"ip:{alice['X-Forwarded-For']}:questions"].append(limits.clock())
        limited = client.post(f"/api/v1/clips/{clip}/chat", json={"question": "hi"}, headers=alice)
        assert limited.status_code == 429


def test_guest_sessions_are_limited_per_address_and_forging_the_header_doesnt_help(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, guest_sessions_per_hour=2)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        for _ in range(2):
            guest(client)
        forged = client.post("/api/v1/auth/guest", headers={"X-Forwarded-For": "8.8.8.8, 41.58.0.7"})
        assert forged.status_code == 429


def test_accounts_have_their_own_limit_and_admins_none(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, user_uploads_per_hour=1)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, clean_db, email="u@test.local", role="user")
        assert client.post("/api/v1/clips", files={"file": VIDEO}).status_code == 201
        assert client.post("/api/v1/clips", files={"file": VIDEO}).status_code == 429
    with TestClient(create_app(settings=settings, start_worker=False)) as admin:
        sign_in(admin, clean_db)
        for _ in range(3):
            assert admin.post("/api/v1/clips", files={"file": VIDEO}).status_code == 201


def test_oversized_uploads_are_refused_before_being_read(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, max_upload_mb=1)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice = guest(client)
        big = client.post("/api/v1/clips", files={"file": ("big.mp4", b"x" * (1024 * 1024 + 2048), "video/mp4")}, headers=alice)
        assert big.status_code == 413 and "1 MB" in big.json()["detail"]
        assert client.post("/api/v1/clips", files={"file": VIDEO}, headers=alice).status_code == 201
