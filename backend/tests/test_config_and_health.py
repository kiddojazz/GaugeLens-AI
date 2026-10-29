from fastapi.testclient import TestClient

from backend.app.config import missing_settings
from backend.app.main import create_app
from backend.tests.fakes import make_settings


def test_missing_settings_need_the_meta_api_key_unless_replaying_the_cache(tmp_path):
    assert missing_settings(make_settings(tmp_path)) == []
    assert missing_settings(make_settings(tmp_path, meta_api_key=None)) == ["MODEL_API_KEY"]
    assert missing_settings(make_settings(tmp_path, meta_api_key=None, cache_only=True)) == []


def test_command_line_modules_import():
    import backend.app.cli  # noqa: F401 - catches syntax errors in code only the CLI reaches
    import eval.spikes.mock_spike  # noqa: F401
    import eval.synth.make_mock_clip  # noqa: F401


def test_read_model_is_the_configured_muse_model(tmp_path):
    assert make_settings(tmp_path).read_model == "muse-spark-1.3"
    assert make_settings(tmp_path, meta_model_read="muse-spark-2").read_model == "muse-spark-2"


def test_health_reports_configuration_without_a_database(tmp_path):
    client = TestClient(create_app(settings=make_settings(tmp_path)))
    body = client.get("/api/v1/health").json()
    assert body["db"] == "not configured" and body["status"] == "degraded"
    assert body["model"] == {"provider": "meta", "model": "muse-spark-1.3", "cache_only": False, "configured": True}
