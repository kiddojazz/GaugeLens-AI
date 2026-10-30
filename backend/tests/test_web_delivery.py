"""How the built UI and JSON are delivered: compressed and cached, which is most of a phone's first load."""

import pytest
from fastapi.testclient import TestClient

from backend.app.config import PROJECT_ROOT
from backend.app.main import create_app
from backend.tests.fakes import make_settings


def test_ui_is_compressed_and_hashed_assets_cached_for_good(tmp_path):
    assets = sorted((PROJECT_ROOT / "web" / "dist" / "assets").glob("*.js"))
    if not assets:
        pytest.skip("web/dist isn't built")
    client = TestClient(create_app(settings=make_settings(tmp_path)))
    js = client.get(f"/assets/{assets[0].name}", headers={"Accept-Encoding": "gzip"})
    assert js.headers["content-encoding"] == "gzip" and "immutable" in js.headers["cache-control"]
    assert client.get("/").headers["cache-control"] == "no-cache"          # a new deploy is picked up at once
