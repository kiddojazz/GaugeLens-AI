"""Automatic onboarding: identification -> reuse or register (nameplate limits, roles, register codes) ->
generic rules for unknown kinds -> a clip uploaded without equipment is identified, then processed."""

import asyncio
from types import SimpleNamespace

from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.events import fetch_after
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient
from backend.app.pipeline import identify
from backend.app.pipeline.clips import create_clip
from backend.app.pipeline.physics_stage import constants_for, pack_for
from backend.app.pipeline.read_digital import TOOL_NAME as READ_TOOL
from backend.app.seed import seed_mock

from .fakes import make_settings, tool_response
from .test_process_clip import CLAMP, INVERTER
from .videos import solid_states_video

NO_RATINGS = {"nominal_voltage_v": None, "phases": None, "max_current_a": None, "rated_power_va": None,
              "battery_voltage_v": None, "flow_q3_m3h": None}


def value(label, quantity, unit, role, code=None, cumulative=False):
    return {"label": label, "quantity": quantity, "unit": unit, "role": role, "register_code": code,
            "cumulative": cumulative}


LATRONIC = {"kind": "meter", "brand": "Latronic", "model": "L20-1Z1-20A0",
            "description": "bidirectional three-phase electricity meter",
            "ratings": NO_RATINGS | {"nominal_voltage_v": 230, "phases": 3, "max_current_a": 60},
            "displays": [{"label": "Meter LCD", "description": "cycles OBIS 1.8.0 import and 2.8.0 export", "values": [
                value("1.8.0", "energy", "kWh", "energy_kwh", "1.8.0", True),
                value("2.8.0", "energy", "kWh", "energy_export_kwh", "2.8.0", True)]}],
            "evidence": ["Latronic", "L20-1Z1-20A0", "3x230/400 V", "0,25-5(60) A"], "confidence": "high"}
WATER = {"kind": "meter", "brand": "Metrona", "model": None, "description": "domestic water meter",
         "ratings": NO_RATINGS | {"flow_q3_m3h": 2.5},
         "displays": [{"label": "Meter LCD", "description": "volume with a decimal comma", "values": [
             value("volume", "volume", "m³", "volume_m3", None, True)]}],
         "evidence": ["METRONA", "Q3 2,5"], "confidence": "medium"}
COUNTER = {"kind": "other", "brand": None, "model": None, "description": "generator run-hour counter",
           "ratings": NO_RATINGS,
           "displays": [{"label": "Hour counter", "description": "running hours", "values": [
               value("h", "running hours", "h", "other", None, True), value("rpm", "speed", "rpm", "other")]}],
           "evidence": [], "confidence": "low"}


def parse(payload):
    return identify.Identification.model_validate(payload)


def with_pool(db_url, steps):
    async def go():
        pool = await create_pool(db_url)
        try:
            return await steps(pool)
        finally:
            await pool.close()
    return asyncio.run(go())


def test_a_new_meter_registers_itself_with_roles_register_codes_and_nameplate_limits(clean_db):
    async def steps(pool):
        equipment_id, created = await identify.register(pool, parse(LATRONIC))
        assert created and equipment_id.startswith("latronic_l20_1z1_20a0_")
        eq = dict(await pool.fetchrow("SELECT * FROM equipment WHERE equipment_id = $1", equipment_id))
        assert eq["kind"] == "meter" and eq["profile"]["source"] == "auto"
        assert eq["profile"]["limits"] == {"max_power_kw": 41.4}                     # 3 x 230 V x 60 A
        assert constants_for(eq)["max_power_kw"] == 41.4                               # reaches the physics rules
        instruments = {r["physics_role"]: dict(r) for r in await pool.fetch(
            "SELECT * FROM instruments WHERE equipment_id = $1", equipment_id)}
        assert set(instruments) == {"energy_kwh", "energy_export_kwh"}
        assert instruments["energy_kwh"]["match"]["strict"] is True                  # import never swallows export
        assert instruments["energy_kwh"]["match"]["page"] == ["1.8.0"]
        display = await pool.fetchrow("SELECT * FROM displays WHERE equipment_id = $1", equipment_id)
        assert display["sam_prompt"] == "digital display" and "OBIS" in display["context"]

        again, created_again = await identify.register(pool, parse(LATRONIC))         # the same meter, next week
        assert (again, created_again) == (equipment_id, False)
        assert await pool.fetchval("SELECT count(*) FROM equipment WHERE profile->>'source' = 'auto'") == 1

        water_id, _ = await identify.register(pool, parse(WATER))
        water = dict(await pool.fetchrow("SELECT * FROM equipment WHERE equipment_id = $1", water_id))
        assert water["profile"]["limits"] == {"max_flow_m3h": 3.125}                 # Q4 = 1.25 x Q3
        assert await pool.fetchval("SELECT unit FROM instruments WHERE equipment_id = $1", water_id) == "m3"
        pack = pack_for(water, [dict(r) for r in await pool.fetch("SELECT * FROM instruments WHERE equipment_id = $1", water_id)])
        assert "meter.flow_rate" in {r.id for r in pack.rules}

    with_pool(clean_db, steps)


def test_an_unknown_device_gets_generic_roles_and_the_generic_counter_check(clean_db):
    async def steps(pool):
        equipment_id, created = await identify.register(pool, parse(COUNTER))
        assert created and equipment_id.startswith("generator_run_hour")
        eq = dict(await pool.fetchrow("SELECT * FROM equipment WHERE equipment_id = $1", equipment_id))
        instruments = [dict(r) for r in await pool.fetch("SELECT * FROM instruments WHERE equipment_id = $1", equipment_id)]
        assert sorted(i["physics_role"] for i in instruments) == ["running_hours_h", "speed_rpm"]
        pack = pack_for(eq, instruments)
        assert [r.id for r in pack.rules] == ["generic.running_hours_h_monotonic"]     # a counter never goes down
        assert pack_for({"kind": "multimeter"}, [{"physics_role": "voltage_v", "unit": "V", "match": {}}]) is None
        assert identify._slug("3-phase power") == "v_3_phase_power"

    with_pool(clean_db, steps)


def test_too_little_printed_text_never_matches_another_unit(clean_db):
    async def steps(pool):
        first, _ = await identify.register(pool, parse(COUNTER))
        second, created = await identify.register(pool, parse(COUNTER))
        assert created and first != second                                          # no brand/model: no match

    with_pool(clean_db, steps)


class IdentifyingFake:
    """Answers the identification call (as the mock inverter), then display reads."""

    def __init__(self, identification):
        self.identification, self.identify_calls = identification, 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        if kwargs["messages"][0]["content"].startswith("You identify equipment"):
            self.identify_calls += 1
            images = [p for p in kwargs["messages"][1]["content"] if p["type"] == "image_url"]
            assert len(images) == 3 and images[0]["image_url"]["url"].startswith("data:image/jpeg")   # start, middle, end
            return tool_response(self.identification, name=identify.TOOL_NAME)
        text = kwargs["messages"][1]["content"][0]["text"]
        return tool_response(CLAMP if "clamp" in text else INVERTER, name=READ_TOOL)


def test_a_clip_uploaded_without_equipment_is_identified_then_processed(tmp_path, clean_db):
    settings = make_settings(tmp_path, database_url=clean_db, evidence_dir=tmp_path / "evidence")
    video = solid_states_video(tmp_path / "clip.mp4", [(30, 30, 30), (180, 180, 180)], seconds_each=1.0)
    mock = {"kind": "inverter", "brand": "Mock", "model": "inverter 3.5 kVA 24 V", "description": "off-grid inverter",
            "ratings": NO_RATINGS | {"rated_power_va": 3500, "battery_voltage_v": 24},
            "displays": [{"label": "Inverter LCD", "description": "battery page", "values": [
                value("BATT", "voltage", "V", "battery_v")]}], "evidence": ["Mock inverter 3.5 kVA 24 V"],
            "confidence": "high"}
    fake = IdentifyingFake(mock)

    async def scenario():
        await seed_mock(clean_db)
        pool = await create_pool(clean_db)
        try:
            evidence = LocalEvidenceStore(settings.evidence_dir)
            clip_id, _ = await create_clip(pool, evidence, PostgresJobQueue(pool), video=video.read_bytes(),
                                           filename="clip.mp4", equipment_id=None)
            assert await pool.fetchval("SELECT equipment_id FROM clips WHERE clip_id = $1", clip_id) is None
            await Worker(pool, settings, client=ChatClient(settings, raw_client=fake), evidence=evidence).run_once()
            clip = dict(await pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", clip_id))
            readings = await pool.fetchval(
                "SELECT count(*) FROM readings r JOIN frames f USING (frame_id) WHERE f.clip_id = $1", clip_id)
            events = [e["type"] for e in await fetch_after(pool, clip_id, 0, limit=500)]
            return clip, readings, events
        finally:
            await pool.close()

    clip, readings, events = asyncio.run(scenario())
    assert fake.identify_calls == 1
    assert clip["equipment_id"] == "inv_mock" and clip["status"] == "done"            # recognised the seeded unit
    assert clip["identification"]["created"] is False and clip["identification"]["confidence"] == "high"
    assert readings > 0 and "identified" in events and events.index("identified") < events.index("clip_done")


def test_a_person_can_correct_what_was_identified(tmp_path, clean_db):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app
    from backend.tests.fakes import sign_in

    async def steps(pool):
        return await identify.register(pool, parse(COUNTER))

    equipment_id, _ = with_pool(clean_db, steps)
    settings = make_settings(tmp_path, database_url=clean_db)
    with TestClient(create_app(settings=settings, start_worker=False)) as client:
        sign_in(client, settings.database_url)
        fixed = client.patch(f"/api/v1/equipment/{equipment_id}", json={"model": "Perkins hour meter", "kind": "generator"})
        assert fixed.status_code == 200
        assert fixed.json()["model"] == "Perkins hour meter" and fixed.json()["kind"] == "generator"
        assert fixed.json()["profile"]["corrected"] is True and fixed.json()["profile"]["source"] == "auto"
        assert client.patch(f"/api/v1/equipment/{equipment_id}", json={"kind": "spaceship"}).status_code == 422
        assert client.patch("/api/v1/equipment/nope", json={"model": "x"}).status_code == 404
        upload = client.post("/api/v1/clips", files={"file": ("c.mp4", b"x", "video/mp4")})   # no equipment field
        assert upload.status_code == 201
        scan = client.post("/api/v1/clips", files={"file": ("scan.webm", b"x", "video/webm")},
                           data={"equipment_id": "auto", "simulated": "true"})               # the Scan page
        assert scan.status_code == 201
        detail = client.get(f"/api/v1/clips/{scan.json()['clip_id']}").json()["clip"]
        assert detail["equipment_id"] is None and detail["is_demo_replay"] is True
