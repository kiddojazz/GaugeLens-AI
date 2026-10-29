"""The read endpoints the web UI uses: clip list, frames with display boxes, checks, corrections, evidence."""

import asyncio

from fastapi.testclient import TestClient

from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.app.pipeline.clips import create_clip
from backend.app.seed import seed_mock
from backend.tests.test_process_clip import CLAMP, OUTPUT_PAGE_MISREAD, DigitSam, RelookAwareDisplays, setup


def test_ui_endpoints_show_a_processed_clip_and_its_correction(tmp_path, clean_db):
    settings, _, video = setup(tmp_path, clean_db)

    async def process():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            clip_id, _ = await create_clip(pool, evidence, PostgresJobQueue(pool), video=video.read_bytes(),
                                           filename="clip.mp4", equipment_id="inv_mock")
            await Worker(pool, settings, client=ChatClient(settings, raw_client=RelookAwareDisplays(OUTPUT_PAGE_MISREAD, CLAMP)),
                         evidence=evidence, sam=DigitSam()).run_once()
            return clip_id
        finally:
            await pool.close()

    clip_id = asyncio.run(process())
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        clips = client.get("/api/v1/clips").json()
        assert clips[0]["clip_id"] == clip_id and clips[0]["frames"] == 4 and clips[0]["corrections"] >= 1

        frames = client.get(f"/api/v1/clips/{clip_id}/frames").json()
        assert len(frames["frames"]) == 4 and frames["frame_size"] == {"w": 1400, "h": 820}
        assert {b["display_id"] for b in frames["frames"][0]["boxes"]} == {"inv_mock_lcd", "clamp_mock_lcd"}

        checks = client.get(f"/api/v1/clips/{clip_id}/checks").json()
        assert any(c["outcome"] == "misread_fixed" for c in checks)
        assert all(c["outcome"] != "skipped" for c in checks)
        assert any(c["outcome"] == "skipped" for c in client.get(f"/api/v1/clips/{clip_id}/checks?include_skipped=true").json())

        fixes = client.get(f"/api/v1/corrections?clip_id={clip_id}").json()
        assert fixes[0]["original_text"] == "838" and fixes[0]["fixed_text"] == "338" and fixes[0]["role"] == "output_w"
        image = client.get("/api/v1/evidence", params={"uri": fixes[0]["fixed_crop"]})
        assert image.status_code == 200 and image.content[:4] == b"\x89PNG"

        assert client.get("/api/v1/evidence", params={"uri": "../.env"}).status_code == 400   # no path escapes
        assert client.get("/api/v1/evidence", params={"uri": "clip=nope/x.png"}).status_code == 404
