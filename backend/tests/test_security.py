"""Regressions from the security audit."""

import io
import logging

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import access
from backend.app.llm.sam import parse_sam_output
from backend.app.main import HideSessionTokens, create_app
from backend.app.pipeline import ingest
from backend.tests.fakes import make_settings, sign_in

VIDEO = ("v.mp4", b"SECRET-OF-B", "video/mp4")


def guest(client) -> dict:
    return {access.HEADER: client.post("/api/v1/auth/guest").json()["token"]}


def test_a_dotdot_path_cant_reach_another_persons_clip(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence")
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        alice, bob = guest(client), guest(client)
        mine = client.post("/api/v1/clips", files={"file": VIDEO}, headers=alice).json()["clip_id"]
        theirs = client.post("/api/v1/clips", files={"file": VIDEO}, headers=bob).json()["clip_id"]
        secret = f"clip={theirs}/source.mp4"
        assert client.get("/api/v1/evidence", params={"uri": secret}, headers=bob).content == b"SECRET-OF-B"
        for uri in (f"clip={mine}/../{secret}", f"clip={mine}/./../{secret}", f"clip={mine}/x/../../{secret}",
                    f"clip={mine}\\..\\{secret}", f"clip={mine}//../{secret}", f"/{secret}"):
            attack = client.get("/api/v1/evidence", params={"uri": uri}, headers=alice)
            assert attack.status_code == 400 and b"SECRET-OF-B" not in attack.content, uri
        assert client.get("/api/v1/evidence", params={"uri": secret}, headers=alice).status_code == 404
        assert client.get("/api/v1/evidence", params={"uri": f"clip={mine}/source.mp4"}, headers=alice).status_code == 200


def test_only_an_admin_can_make_health_spend_model_credits(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    pings = []

    class Client:
        async def ping(self):
            pings.append(1)
            return "ok", 5

    with TestClient(create_app(settings=settings, client=Client(), start_worker=False)) as client:
        assert "ping_ms" not in client.get("/api/v1/health?deep=true").json()["model"] and not pings
        sign_in(client, clean_db, email="u@test.local", role="user")
        assert "ping_ms" not in client.get("/api/v1/health?deep=true").json()["model"] and not pings
    with TestClient(create_app(settings=settings, client=Client(), start_worker=False)) as admin:
        sign_in(admin, clean_db)
        assert admin.get("/api/v1/health?deep=true").json()["model"]["ping_ms"] == 5 and pings == [1]


def test_an_image_bomb_is_refused_from_its_header(monkeypatch):
    out = io.BytesIO()
    Image.new("RGB", (2000, 2000), "white").save(out, "PNG")
    monkeypatch.setattr(ingest, "MAX_PHOTO_PIXELS", 1_000_000)
    load = Image.Image.load
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("decoded a photo over the limit") or load(self))
    with pytest.raises(ValueError, match="too large"):
        ingest.normalise_photo(out.getvalue())


def test_a_bad_sam_record_is_skipped_not_fatal():
    good = "<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10;c=0.9|><|mask;x=0;y=0;c=0.9;data=1,1,!!|>"
    assert len(parse_sam_output(good)) == 1                                  # the format the model really returns
    assert parse_sam_output("complete garbage") == []
    for bad in (good.replace("x1=1", "x1=oops"), good.replace("w=10;", ""), good.replace("c=0.9", "c=high")):
        assert parse_sam_output(bad) == [], bad
    two = good + "," + "1<|box;x1=1;y1=2;x2=3;y2=4;w=x;h=10|><|mask;data=1|>"
    assert [d.object_id for d in parse_sam_output(two)] == [0]               # the good record survives a bad neighbour


def test_session_tokens_stay_out_of_access_logs():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4", "GET", "/api/v1/evidence?uri=clip=c/a.jpg&s=SECRETTOKEN", "1.1", 200), None)
    assert HideSessionTokens().filter(record)
    assert "SECRETTOKEN" not in record.getMessage() and "uri=clip=c/a.jpg" in record.getMessage()


def test_responses_carry_the_security_headers(tmp_path):
    response = TestClient(create_app(settings=make_settings(tmp_path))).get("/api/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff" and response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
