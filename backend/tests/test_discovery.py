"""SAM 3.1 display discovery: matching detections to registered displays, and the pipeline using it."""

import asyncio
from dataclasses import replace

from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.events import fetch_after
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient, ModelUnavailable
from backend.app.llm.sam import Detection
from backend.app.pipeline.discovery import match_detections
from backend.app.seed import seed_mock
from backend.app.pipeline.clips import create_clip
from backend.tests.test_process_clip import DisplayAwareFake, setup

FRAME = (1400, 820)
INVERTER = {"display_id": "inv", "roi": {"x": 245, "y": 238, "w": 640, "h": 255}, "roi_frame_size": {"w": 1400, "h": 820},
            "layout_rank": 0}
CLAMP = {"display_id": "clamp", "roi": {"x": 975, "y": 302, "w": 186, "h": 108}, "roi_frame_size": {"w": 1400, "h": 820},
         "layout_rank": 1}
SAM_INVERTER = Detection(0, 0, (244, 238, 884, 492), FRAME, 0.81)        # real SAM 3.1 boxes (Spike C)
SAM_CLAMP = Detection(1, 0, (973, 301, 1161, 411), FRAME, 0.73)


def test_manual_rois_pick_the_overlapping_detection():
    matches = match_detections([INVERTER, CLAMP], [SAM_CLAMP, SAM_INVERTER], FRAME)
    assert matches["inv"] is SAM_INVERTER and matches["clamp"] is SAM_CLAMP


def test_without_manual_rois_displays_are_matched_left_to_right():
    no_roi = [{**INVERTER, "roi": None}, {**CLAMP, "roi": None}]
    matches = match_detections(no_roi, [SAM_CLAMP, SAM_INVERTER], FRAME)
    assert matches["inv"] is SAM_INVERTER and matches["clamp"] is SAM_CLAMP


def test_moved_camera_still_matches_by_order_when_manual_rois_no_longer_overlap():
    shifted = [replace(SAM_INVERTER, box=(44, 38, 684, 292)), replace(SAM_CLAMP, box=(773, 101, 961, 211))]
    matches = match_detections([INVERTER, CLAMP], shifted, FRAME)
    assert matches["inv"].box[0] == 44 and matches["clamp"].box[0] == 773


def test_extra_detections_keep_the_most_confident():
    junk = Detection(7, 0, (10, 10, 60, 40), FRAME, 0.31)                  # a reflection somewhere
    matches = match_detections([{**INVERTER, "roi": None}, {**CLAMP, "roi": None}], [junk, SAM_INVERTER, SAM_CLAMP], FRAME)
    assert set(matches) == {"inv", "clamp"} and junk not in matches.values()


def test_one_display_without_rank_or_roi_takes_the_only_detection():
    matches = match_detections([{"display_id": "solo", "roi": None, "layout_rank": None}], [SAM_CLAMP], FRAME)
    assert matches["solo"] is SAM_CLAMP


class FakeSam:
    def __init__(self, detections=None, error=None):
        self.detections, self.error, self.calls = detections, error, 0

    async def segment_image(self, image, prompt, mime="image/png"):
        from backend.app.llm import CallMeta
        self.calls += 1
        if self.error:
            raise self.error
        return self.detections, CallMeta(call_id=f"call_sam{self.calls}", provider="meta", model="sam-3.1",
                                         purpose="sam_image", prompt_version="sam_v1", latency_ms=600)


def run_clip(tmp_path, db_url, sam, seg_provider="meta"):
    settings, _, video = setup(tmp_path, db_url)
    settings = replace(settings, seg_provider=seg_provider)

    async def scenario():
        await seed_mock(db_url)
        pool = await create_pool(db_url)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            clip_id, _ = await create_clip(pool, evidence, PostgresJobQueue(pool), video=video.read_bytes(),
                                           filename="clip.mp4", equipment_id="inv_mock")
            await Worker(pool, settings, client=ChatClient(settings, raw_client=DisplayAwareFake()), evidence=evidence,
                         sam=sam).run_once()
            rois = {r["display_id"]: dict(r) for r in await pool.fetch("SELECT * FROM clip_rois WHERE clip_id = $1", clip_id)}
            events = await fetch_after(pool, clip_id, 0, limit=500)
            readings = await pool.fetchval("SELECT count(*) FROM readings")
            calls = await pool.fetchval("SELECT count(*) FROM model_calls WHERE model = 'sam-3.1'")
            return rois, events, readings, calls
        finally:
            await pool.close()

    return asyncio.run(scenario())


class TrackingSam(FakeSam):
    """SAM 3.1 video: the inverter drifts 10 px right per source frame-second; the clamp stays put."""

    def __init__(self):
        super().__init__([SAM_INVERTER, SAM_CLAMP])
        self.video_calls = 0

    async def track_video(self, video_path, prompt):
        from backend.app.llm import CallMeta
        self.video_calls += 1
        dets = []
        for frame in range(0, 20):                                      # 10 fps test clip, 2 s
            dx = frame                                                  # +1 px per source frame
            dets.append(Detection(0, frame, (244 + dx, 238, 884 + dx, 492), FRAME, 0.8))
            dets.append(Detection(1, frame, (973, 301, 1161, 411), FRAME, 0.7))
        return dets, CallMeta(call_id="call_track", provider="meta", model="sam-3.1", purpose="sam_video",
                              prompt_version="sam_v1", latency_ms=2000)


def test_tracking_gives_every_frame_its_own_box(tmp_path, clean_db):
    sam = TrackingSam()
    rois, events, readings, calls = run_clip(tmp_path, clean_db, sam)
    assert sam.video_calls == 1 and sam.calls == 0                      # tracked, no single-image call
    track = rois["inv_mock_lcd"]["track"]
    assert track["0"] == [244, 238, 640, 254] and track["2"] == [254, 238, 640, 254]   # t=1.0 s -> frame 10
    assert rois["clamp_mock_lcd"]["track"]["3"] == [973, 301, 188, 110]
    detected = next(e["payload"] for e in events if e["type"] == "displays_detected")
    assert detected["tracked"] is True and readings == 12


def test_pipeline_uses_sam_regions_and_records_them_per_clip(tmp_path, clean_db):
    sam = FakeSam([SAM_INVERTER, SAM_CLAMP])
    rois, events, readings, calls = run_clip(tmp_path, clean_db, sam)
    assert sam.calls == 1 and calls == 1                                   # one SAM call per clip, logged
    assert rois["inv_mock_lcd"]["source"] == "sam" and rois["inv_mock_lcd"]["roi"] == {"x": 244, "y": 238, "w": 640, "h": 254}
    assert rois["clamp_mock_lcd"]["source"] == "sam" and round(rois["clamp_mock_lcd"]["confidence"], 2) == 0.73
    detected = next(e["payload"] for e in events if e["type"] == "displays_detected")
    assert {d["source"] for d in detected["displays"]} == {"sam"}
    assert readings == 12


def test_sam_failure_falls_back_to_manual_regions(tmp_path, clean_db):
    rois, events, readings, _ = run_clip(tmp_path, clean_db, FakeSam(error=ModelUnavailable("SAM down")))
    assert {r["source"] for r in rois.values()} == {"manual"} and readings == 12


def test_manual_mode_never_calls_sam(tmp_path, clean_db):
    sam = FakeSam([SAM_INVERTER, SAM_CLAMP])
    rois, _, _, _ = run_clip(tmp_path, clean_db, sam, seg_provider="manual")
    assert sam.calls == 0 and {r["source"] for r in rois.values()} == {"manual"}
