"""Photos as clips (each photo one frame), the frame cap for long videos, and re-looks on a frame not yet saved."""

import asyncio
import io

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient
from backend.app.main import create_app
from backend.app.pipeline import identify
from backend.app.pipeline.clips import ClipError, create_clip, create_photo_clip
from backend.app.pipeline.ingest import PHOTO_GAP_S, effective_fps, normalise_photo, photo_times, sample_frames
from backend.app.pipeline.physics_stage import PhysicsStage
from backend.app.seed import seed_mock
from backend.tests.fakes import make_settings, sign_in
from backend.tests.test_identify import LATRONIC, parse
from backend.tests.test_process_clip import CLAMP, OUTPUT_PAGE_MISREAD, DigitSam, RelookAwareDisplays, ScriptedDisplays, setup
from backend.tests.videos import solid_states_video


def phone_photo(size=(400, 300), orientation: int | None = None, taken: str | None = None, gps: bool = False) -> bytes:
    img = Image.new("RGB", size, "white")
    img.paste((200, 0, 0), (0, 0, size[0] // 2, size[1] // 2))          # a red top-left corner, to see rotation
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if taken:
        exif[0x0132] = taken
    if gps:
        exif[0x8825] = {1: "N", 2: (6.0, 27.0, 0.0)}                     # GPSInfo: Lagos-ish
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_photos_are_turned_upright_shrunk_and_stripped_of_metadata():
    jpeg, taken = normalise_photo(phone_photo((4000, 3000), orientation=6, taken="2026:10:29 21:40:00", gps=True))
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (1920, 2560)                       # rotated 90 degrees (portrait), long side 2560
    assert dict(img.getexif()) == {}                      # no location, no camera details
    assert taken is not None and taken.minute == 40
    try:
        normalise_photo(b"not an image")
    except ValueError as exc:
        assert "JPEG, PNG or WebP" in str(exc)
    else:
        raise AssertionError("expected a ValueError")


def test_photo_times_are_real_only_when_every_photo_has_one():
    from datetime import datetime
    a, b = datetime(2026, 10, 29, 21, 40), datetime(2026, 10, 29, 21, 42)
    assert photo_times([a, b]) == ([0.0, 120.0], True)
    assert photo_times([a, None]) == ([0.0, PHOTO_GAP_S], False)
    assert photo_times([a, a]) == ([0.0, PHOTO_GAP_S], False)   # duplicates can't be ordered in time


def test_long_videos_are_sampled_more_sparsely(tmp_path):
    video = solid_states_video(tmp_path / "long.mp4", [(30, 30, 30), (180, 180, 180)], seconds_each=5.0)
    assert effective_fps(str(video), 2.0, None) == 2.0
    fps = effective_fps(str(video), 2.0, 6)
    assert abs(fps - 0.6) < 0.05
    assert len(sample_frames(str(video), fps)) <= 7
    assert effective_fps(str(video), 2.0, 100) == 2.0      # short enough already


def test_a_photo_clip_is_read_frame_by_frame_with_per_photo_frames(tmp_path, clean_db):
    settings, _, video = setup(tmp_path, clean_db)
    photos = [(f"p{n}.jpg", f.jpeg) for n, f in enumerate(sample_frames(str(video), 2.0)[:3], start=1)]

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            clip_id, _ = await create_photo_clip(pool, evidence, PostgresJobQueue(pool), photos=photos,
                                                 equipment_id="inv_mock")
            fake = ScriptedDisplays(OUTPUT_PAGE_MISREAD, CLAMP)
            await Worker(pool, settings, client=ChatClient(settings, raw_client=fake), evidence=evidence).run_once()
            clip = await pool.fetchrow("SELECT status, fps_sampled, media FROM clips WHERE clip_id = $1", clip_id)
            assert clip["status"] == "done" and clip["fps_sampled"] is None
            assert clip["media"]["kind"] == "photos" and clip["media"]["timed"] is False
            frames = await pool.fetch("SELECT t_offset_s FROM frames WHERE clip_id = $1 ORDER BY t_offset_s", clip_id)
            assert [float(f["t_offset_s"]) for f in frames] == [0.0, PHOTO_GAP_S, 2 * PHOTO_GAP_S]
            readings = await pool.fetchval("""SELECT count(*) FROM readings r JOIN frames f USING (frame_id)
                                              WHERE f.clip_id = $1""", clip_id)
            assert readings > 0
            # single-moment physics still runs on a photo: W can't exceed VA
            assert await pool.fetchval("SELECT count(*) FROM physics_checks WHERE clip_id = $1 "
                                       "AND rule_id = 'inverter.power_factor_bound' AND NOT passed", clip_id) >= 1
            for photo in clip["media"]["photos"]:
                assert evidence.exists(photo["uri"])
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_photos_without_times_skip_rate_rules_but_keep_the_rest(clean_db):
    async def steps():
        pool = await create_pool(clean_db)
        try:
            meter, _ = await identify.register(pool, parse(LATRONIC))
            timed = await PhysicsStage.load(pool, {"equipment_id": meter}, timed=True)
            untimed = await PhysicsStage.load(pool, {"equipment_id": meter}, timed=False)
            return {r.id for r in timed.pack.rules}, {r.id for r in untimed.pack.rules}
        finally:
            await pool.close()

    timed, untimed = asyncio.run(steps())
    assert "meter.energy_rate" in timed and "meter.energy_rate" not in untimed
    assert "meter.energy_monotonic" in untimed                 # counters are still compared in photo order


def test_upload_takes_one_video_or_up_to_ten_photos(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db)
    jpeg = phone_photo()
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, clean_db)
        two = client.post("/api/v1/clips", files=[("file", ("a.jpg", jpeg, "image/jpeg")),
                                                   ("file", ("b.JPG", jpeg, "image/jpeg"))])
        assert two.status_code == 201, two.text
        media = client.get(f"/api/v1/clips/{two.json()['clip_id']}").json()["clip"]["media"]
        assert media["kind"] == "photos" and len(media["photos"]) == 2

        mixed = client.post("/api/v1/clips", files=[("file", ("a.jpg", jpeg, "image/jpeg")),
                                                     ("file", ("v.mp4", b"video", "video/mp4"))])
        assert mixed.status_code == 400 and "not both" in mixed.json()["detail"]
        heic = client.post("/api/v1/clips", files={"file": ("IMG_1.HEIC", b"heic", "image/heic")})
        assert heic.status_code == 400 and "Most Compatible" in heic.json()["detail"]
        broken = client.post("/api/v1/clips", files={"file": ("x.png", b"not a png", "image/png")})
        assert broken.status_code == 400
        eleven = client.post("/api/v1/clips", files=[("file", (f"{n}.jpg", jpeg, "image/jpeg")) for n in range(11)])
        assert eleven.status_code == 400 and "at most 10" in eleven.json()["detail"]


class StillFailingFirstRelook(RelookAwareDisplays):
    """The first re-look reads another value that still breaks physics (900 W > 393 VA), so attempt 3 runs, on the
    neighbouring (next) frame."""

    relooks = 0

    async def create(self, **kwargs):
        if kwargs["messages"][0]["content"].startswith("You re-read numbers"):
            StillFailingFirstRelook.relooks += 1
            if StillFailingFirstRelook.relooks == 1:
                from backend.tests.fakes import tool_response
                self.calls += 1
                items = [{"image": 1, "value": 900, "unit": "W", "raw_text": "900", "legible": True},
                         {"image": 2, "value": 393, "unit": "VA", "raw_text": "393", "legible": True},
                         {"image": 3, "value": 11, "unit": "%", "raw_text": "11", "legible": True}]
                return tool_response({"items": items}, name="record_numbers")
        return await super().create(**kwargs)


def test_a_relook_on_the_next_frame_saves_that_frame_first(tmp_path, clean_db):
    # Regression: attempt 3 re-reads the NEXT frame, which wasn't in the database yet, so storing its reading
    # broke the readings -> frames foreign key and failed the whole clip (it retried from scratch, twice).
    settings, _, video = setup(tmp_path, clean_db)
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence", seg_provider="meta")
    StillFailingFirstRelook.relooks = 0

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            clip_id, job_id = await create_clip(pool, evidence, PostgresJobQueue(pool), video=video.read_bytes(),
                                                filename="clip.mp4", equipment_id="inv_mock")
            fake = StillFailingFirstRelook(OUTPUT_PAGE_MISREAD, CLAMP)
            await Worker(pool, settings, client=ChatClient(settings, raw_client=fake), evidence=evidence,
                         sam=DigitSam()).run_once()
            job = await pool.fetchrow("SELECT status, attempts, last_error FROM jobs WHERE job_id = $1", job_id)
            assert job["status"] == "done" and job["attempts"] == 1, job["last_error"]
            third = await pool.fetchrow("SELECT frame_id FROM readings WHERE attempt = 3 LIMIT 1")
            assert third is not None and third["frame_id"].endswith("_f00001")   # re-read on the next frame
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_create_photo_clip_needs_photos():
    async def go():
        try:
            await create_photo_clip(None, None, None, photos=[], equipment_id=None)
        except ClipError as exc:
            return str(exc)

    assert asyncio.run(go()) == "no photos"
