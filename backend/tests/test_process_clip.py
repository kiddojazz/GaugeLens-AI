"""process_clip end to end on a real Postgres with a scripted fake model (no network)."""

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.events import fetch_after
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.app.pipeline.clips import create_clip
from backend.app.pipeline.read_digital import TOOL_NAME
from backend.app.seed import seed_mock
from backend.tests.fakes import make_settings, tool_response
from backend.tests.videos import solid_states_video

INVERTER = {"display_kind": "seven_segment", "page_hint": "battery", "glare_or_occlusion": False,
            "fields": [{"label": "BATT", "value": 23.4, "unit": "V", "raw_text": "23.4", "legible": True},
                       {"label": "BATT", "value": None, "unit": "%", "raw_text": "3?", "legible": False,
                        "reason_if_illegible": "glare"}]}
CLAMP = {"display_kind": "seven_segment", "page_hint": None, "glare_or_occlusion": False,
         "fields": [{"label": "DC A", "value": 17.2, "unit": "A", "raw_text": "17.2", "legible": True},
                    {"label": "", "value": 88, "unit": "kWh", "raw_text": "88", "legible": True}]}


class DisplayAwareFake:
    """Answers by which display is being read (the two displays of a frame are read concurrently)."""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls += 1
        text = kwargs["messages"][1]["content"][0]["text"]
        return tool_response(CLAMP if "clamp" in text else INVERTER, name=TOOL_NAME)


def setup(tmp_path, db_url):
    settings = make_settings(tmp_path, database_url=db_url, evidence_dir=tmp_path / "evidence")
    fake = DisplayAwareFake()
    video = solid_states_video(tmp_path / "clip.mp4", [(30, 30, 30), (180, 180, 180)], seconds_each=1.0)
    return settings, fake, video


def test_clip_becomes_readings_with_lineage_reuse_and_events(tmp_path, clean_db):
    settings, fake, video = setup(tmp_path, clean_db)

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            queue = PostgresJobQueue(pool)
            clip_id, job_id = await create_clip(pool, evidence, queue, video=video.read_bytes(), filename="clip.mp4",
                                                equipment_id="inv_mock")
            worker = Worker(pool, settings, client=ChatClient(settings, raw_client=fake), evidence=evidence)
            assert (await worker.run_once()).job_id == job_id
            assert (await queue.get(job_id))["status"] == "done"
            assert await pool.fetchval("SELECT status FROM clips WHERE clip_id = $1", clip_id) == "done"

            # 4 frames (t = 0, 0.5, 1.0, 1.5); each display is read at t=0 and t=1.0, reused at 0.5 and 1.5
            assert await pool.fetchval("SELECT count(*) FROM frames WHERE clip_id = $1", clip_id) == 4
            assert fake.calls == 4 and await pool.fetchval("SELECT count(*) FROM model_calls") == 4
            rows = await pool.fetch("""SELECT r.*, i.physics_role FROM readings r JOIN frames f USING (frame_id)
                                       JOIN instruments i USING (instrument_id) WHERE f.clip_id = $1""", clip_id)
            assert len(rows) == 4 * 3                                 # battery V, battery %, clamp A per frame
            assert sum(r["reused_from"] is not None for r in rows) == 6
            soc = [r for r in rows if r["physics_role"] == "battery_soc_pct"]
            assert all(r["status"] == "needs_review" and r["value"] is None and r["confidence"] == 0 for r in soc)
            fresh = [r for r in rows if r["physics_role"] == "battery_v" and r["reused_from"] is None]
            assert all(r["value"] == 23.4 and r["raw_text"] == "23.4" and r["model_call_id"] and
                       r["crop_uri"].endswith("_inv_mock_lcd.png") and evidence.exists(r["crop_uri"]) for r in fresh)

            events = await fetch_after(pool, clip_id, 0, limit=500)
            types = [e["type"] for e in events]
            assert types[0] == "job_status" and types[-2:] == ["clip_done", "job_status"]
            assert types.count("frame_processed") == 4
            done = next(e["payload"] for e in events if e["type"] == "clip_done")
            assert done["frames"] == 4 and done["readings"] == 12
            clamp_read = [d for e in events if e["type"] == "frame_processed"
                          for d in e["payload"]["displays"] if d["display_id"] == "clamp_mock_lcd" and d["status"] == "read"]
            assert clamp_read[0]["unmapped"] == ["=88kWh"]            # unknown field kept visible, not guessed

            # a retried job resumes: nothing is read or stored twice
            await queue.retry(job_id) or await pool.execute("UPDATE jobs SET status = 'queued' WHERE job_id = $1", job_id)
            await worker.run_once()
            assert fake.calls == 4
            assert await pool.fetchval("SELECT count(*) FROM readings") == 12
        finally:
            await pool.close()

    asyncio.run(scenario())


OUTPUT_PAGE_MISREAD = {"display_kind": "seven_segment", "page_hint": "output", "glare_or_occlusion": False,
                       "fields": [{"label": "OUTPUT", "value": 838, "unit": "W", "raw_text": "838", "legible": True},
                                  {"label": "OUTPUT", "value": 393, "unit": "VA", "raw_text": "393", "legible": True},
                                  {"label": "LOAD", "value": 11, "unit": "%", "raw_text": "11", "legible": True}]}


class ScriptedDisplays(DisplayAwareFake):
    def __init__(self, inverter, clamp):
        super().__init__()
        self.inverter, self.clamp = inverter, clamp

    async def create(self, **kwargs):
        self.calls += 1
        text = kwargs["messages"][1]["content"][0]["text"]
        return tool_response(self.clamp if "clamp" in text else self.inverter, name=TOOL_NAME)


def test_physics_runs_in_the_pipeline_and_flags_the_misread_reading(tmp_path, clean_db):
    # 338 W read as 838 W (3 -> 8). W can't exceed VA, so physics fails, and attribution must name the
    # watts reading with the fix 338, while the VA and load % readings are accepted.
    settings, _, video = setup(tmp_path, clean_db)
    fake = ScriptedDisplays(OUTPUT_PAGE_MISREAD, CLAMP)

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            queue = PostgresJobQueue(pool)
            clip_id, _ = await create_clip(pool, evidence, queue, video=video.read_bytes(), filename="clip.mp4",
                                           equipment_id="inv_mock")
            await Worker(pool, settings, client=ChatClient(settings, raw_client=fake), evidence=evidence).run_once()

            fails = await pool.fetch("SELECT * FROM physics_checks WHERE clip_id = $1 AND NOT passed AND outcome <> 'skipped'",
                                     clip_id)
            assert {f["rule_id"] for f in fails} == {"inverter.power_factor_bound"}
            first = min(fails, key=lambda f: f["ts"])
            suspect = await pool.fetchrow("""SELECT r.status, r.value, i.physics_role FROM readings r
                                             JOIN instruments i USING (instrument_id) WHERE r.reading_id = $1""",
                                          first["suspect_reading_id"])
            # The re-look (no SAM here: tight-crop fallback) reads 838 again: the value is CONFIRMED, so the
            # violation is treated as real and goes to the anomaly path.
            assert suspect["physics_role"] == "output_w" and suspect["value"] == 838 and suspect["status"] == "accepted"
            assert first["candidate_fix"] == {"role": "output_w", "reading_id": first["suspect_reading_id"],
                                              "value": 338.0, "kind": "seg_8to3"}
            assert first["outcome"] == "persistent_violation" and first["reason"] == "output_w <= output_va * 1.03"
            assert first["relook"]["attempts"][0]["strategy"] == "tight_crop"
            assert first["relook"]["attempts"][0]["outcome"] == "confirmed"
            reused = await pool.fetch(
                """SELECT r.status FROM readings r JOIN frames f USING (frame_id) JOIN instruments i USING (instrument_id)
                   WHERE f.clip_id = $1 AND i.physics_role = 'output_w' AND r.reused_from IS NOT NULL""", clip_id)
            assert reused and {r["status"] for r in reused} == {"accepted"}   # reused copies inherit the outcome

            statuses = dict(await pool.fetch(
                """SELECT i.physics_role, r.status FROM readings r JOIN instruments i USING (instrument_id)
                   JOIN frames f USING (frame_id) WHERE f.clip_id = $1 AND r.reused_from IS NULL""", clip_id))
            assert statuses["output_va"] == "accepted" and statuses["load_pct"] == "accepted"

            skipped = await pool.fetchrow("""SELECT reason FROM physics_checks WHERE clip_id = $1
                                             AND rule_id = 'inverter.dc_power_balance' LIMIT 1""", clip_id)
            assert "missing inputs: battery_v" in skipped["reason"]           # this page shows no battery voltage

            events = await fetch_after(pool, clip_id, 0, limit=1000)
            physics_events = [e["payload"] for e in events if e["type"] == "physics_check"]
            assert any(p["status"] == "fail" and p["candidate_fix"]["value"] == 338.0 for p in physics_events)
            done = next(e["payload"] for e in events if e["type"] == "clip_done")
            assert done["checks_fail"] >= 1 and done["checks_pass"] >= 1
        finally:
            await pool.close()

    asyncio.run(scenario())


class RelookAwareDisplays(ScriptedDisplays):
    """First reads give the misread; the re-look (one crop per number) reads the true values."""

    async def create(self, **kwargs):
        if kwargs["messages"][0]["content"].startswith("You re-read numbers"):
            self.calls += 1
            items = [{"image": 1, "value": 338, "unit": "W", "raw_text": "338", "legible": True},
                     {"image": 2, "value": 393, "unit": "VA", "raw_text": "393", "legible": True},
                     {"image": 3, "value": 11, "unit": "%", "raw_text": "11", "legible": True}]
            return tool_response({"items": items}, name="record_numbers")
        return await super().create(**kwargs)


class DigitSam:
    """SAM 'number' on the inverter crop: three big W digits, three small VA digits, two small % digits."""

    def __init__(self):
        self.prompts = []

    async def segment_image(self, image, prompt, mime="image/png"):
        from backend.app.llm import CallMeta
        from backend.app.llm.sam import Detection
        self.prompts.append(prompt)
        size = (717, 285)
        boxes = [(290, 56, 351, 165), (379, 56, 440, 165), (463, 58, 531, 162),        # 3 3 8  (W)
                 (67, 183, 92, 228), (101, 183, 129, 228), (139, 183, 167, 228),       # 3 9 3  (VA)
                 (520, 183, 527, 228), (550, 183, 557, 228)]                           # 1 1    (%)
        dets = [Detection(i, 0, b, size, 0.8) for i, b in enumerate(boxes)] if prompt == "number" else []
        return dets, CallMeta(call_id=f"call_sam_{len(self.prompts)}", provider="meta", model="sam-3.1",
                              purpose="sam_image", prompt_version="sam_v1", latency_ms=500)


def test_relook_with_sam_digits_fixes_the_misread_and_keeps_the_evidence(tmp_path, clean_db):
    settings, _, video = setup(tmp_path, clean_db)
    fake, sam = RelookAwareDisplays(OUTPUT_PAGE_MISREAD, CLAMP), DigitSam()

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            clip_id, _ = await create_clip(pool, evidence, PostgresJobQueue(pool), video=video.read_bytes(),
                                           filename="clip.mp4", equipment_id="inv_mock")
            await Worker(pool, settings, client=ChatClient(settings, raw_client=fake), evidence=evidence,
                         sam=sam).run_once()
            assert "number" in sam.prompts                                     # SAM boxed the digits

            fixed = await pool.fetch("SELECT * FROM physics_checks WHERE clip_id = $1 AND outcome = 'misread_fixed'",
                                     clip_id)
            assert fixed and fixed[0]["rule_id"] == "inverter.power_factor_bound"
            attempt = fixed[0]["relook"]["attempts"][0]
            assert attempt["strategy"] == "sam_digits" and attempt["value"] == 338 and attempt["outcome"] == "fixed"
            assert evidence.exists(attempt["crop_uri"])                        # attempt-2 crop kept as evidence

            original = await pool.fetchrow("SELECT status, value, crop_uri FROM readings WHERE reading_id = $1",
                                           fixed[0]["suspect_reading_id"])
            corrected = await pool.fetchrow("SELECT * FROM readings WHERE supersedes = $1", fixed[0]["suspect_reading_id"])
            assert original["status"] == "rejected" and original["value"] == 838
            assert corrected["status"] == "corrected" and corrected["value"] == 338 and corrected["attempt"] == 2

            rechecked = await pool.fetch("""SELECT passed FROM physics_checks WHERE clip_id = $1 AND frame_id = $2
                                            AND rule_id = 'inverter.power_factor_bound' ORDER BY ts""",
                                         clip_id, fixed[0]["frame_id"])
            assert [r["passed"] for r in rechecked] == [False, True]          # failed, then passed once corrected

            reused = await pool.fetchrow(
                """SELECT r.value, r.status FROM readings r JOIN frames f USING (frame_id)
                   JOIN instruments i USING (instrument_id)
                   WHERE f.clip_id = $1 AND f.t_offset_s = 0.5 AND i.physics_role = 'output_w'""", clip_id)
            assert reused["value"] == 338 and reused["status"] == "corrected"  # later frames carry the fix

            events = [e for e in await fetch_after(pool, clip_id, 0, limit=1000) if e["type"] == "relook_attempt"]
            assert events and events[0]["payload"]["original_value"] == 838 and events[0]["payload"]["value"] == 338
            done = next(e["payload"] for e in await fetch_after(pool, clip_id, 0, limit=1000) if e["type"] == "clip_done")
            assert done["misread_fixed"] >= 1 and done["relooks"] >= 1
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_failing_reads_are_recorded_and_the_clip_still_finishes(tmp_path, clean_db):
    settings, _, video = setup(tmp_path, clean_db)

    class Down:
        chat = None

        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            raise TimeoutError("model down")

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            queue = PostgresJobQueue(pool)
            clip_id, job_id = await create_clip(pool, evidence, queue, video=video.read_bytes(), filename="clip.mp4",
                                                equipment_id="inv_mock")
            await Worker(pool, settings, client=ChatClient(settings, raw_client=Down()), evidence=evidence).run_once()
            assert (await queue.get(job_id))["status"] == "done"
            types = [e["type"] for e in await fetch_after(pool, clip_id, 0, limit=500)]
            assert types.count("read_failed") == 8 and "clip_done" in types
            assert await pool.fetchval("SELECT count(*) FROM readings") == 0
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_upload_api_queues_the_clip_and_streams_events(tmp_path, clean_db):
    settings, _, video = setup(tmp_path, clean_db)
    asyncio.run(seed_mock(clean_db))
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        assert client.get("/api/v1/health").json()["db"] == "ok"
        response = client.post("/api/v1/clips", data={"equipment_id": "inv_mock"},
                               files={"file": ("clip.mp4", video.read_bytes(), "video/mp4")})
        assert response.status_code == 201
        clip_id = response.json()["clip_id"]
        body = client.get(f"/api/v1/clips/{clip_id}").json()
        assert body["clip"]["status"] == "queued" and body["job"]["status"] == "queued"

        stream = client.get(f"/api/v1/clips/{clip_id}/events?follow=false").text
        assert "event: job_status" in stream and json.loads(stream.split("data: ")[1].split("\n")[0])["status"] == "queued"

        assert client.post("/api/v1/clips", data={"equipment_id": "nope"},
                           files={"file": ("clip.mp4", b"x", "video/mp4")}).status_code == 400
        assert client.post("/api/v1/clips", data={"equipment_id": "inv_mock"},
                           files={"file": ("notes.txt", b"x", "text/plain")}).status_code == 400
        equipment = client.get("/api/v1/equipment").json()
        assert [e["equipment_id"] for e in equipment] == ["clamp_mock", "inv_mock"]
        assert len(equipment[1]["displays"][0]["instruments"]) == 7
