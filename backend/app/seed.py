"""Registry seeds (idempotent).

    python -m backend.app.seed mock     # the mock panel: inverter LCD + DC clamp meter

Mock-panel ROIs are in the pixel space of eval/synth/make_mock_clip.py videos (1400x820) and are scaled
to each clip's resolution. Real equipment gets its ROIs from the registry UI (manual) or SAM 3.1.
"""

import asyncio
import sys

from .config import get_settings
from .db.pool import create_pool

MOCK = {
    "site": ("demo-lagos", "Demo site (mock panel)", "Lagos"),
    "equipment": [
        # equipment_id, kind, model, nominal_v, rated_va, idle_w, linked
        ("inv_mock", "inverter", "Mock inverter 3.5 kVA 24 V", 24, 3500, 35, None),
        ("clamp_mock", "clamp_meter", "Mock DC clamp meter", None, None, None, "inv_mock"),
    ],
    "displays": [
        # display_id, equipment_id, label, roi, context (never expected values)
        ("inv_mock_lcd", "inv_mock", "Inverter LCD", {"x": 245, "y": 238, "w": 640, "h": 255}, "off-grid inverter display"),
        ("clamp_mock_lcd", "clamp_mock", "Clamp meter LCD", {"x": 975, "y": 302, "w": 186, "h": 108}, "DC clamp meter display"),
    ],
    "instruments": [
        # role, display, label, quantity, unit, resolution, match
        ("battery_v", "inv_mock_lcd", "Battery voltage", "voltage", "V", 0.1, {"unit": "V", "page": ["batt"], "label": ["batt"]}),
        ("battery_soc_pct", "inv_mock_lcd", "Battery charge", "state_of_charge", "%", 1, {"unit": "%", "page": ["batt"], "label": ["batt"]}),
        ("output_w", "inv_mock_lcd", "Output power", "power", "W", 1, {"unit": "W"}),
        ("output_va", "inv_mock_lcd", "Output apparent power", "apparent_power", "VA", 1, {"unit": "VA"}),
        ("load_pct", "inv_mock_lcd", "Load", "load", "%", 1, {"unit": "%", "page": ["output", "load"], "label": ["load", "output"]}),
        ("ac_v", "inv_mock_lcd", "AC output voltage", "voltage", "V", 1, {"unit": "V", "page": ["ac"], "label": ["ac"]}),
        ("ac_hz", "inv_mock_lcd", "AC output frequency", "frequency", "Hz", 0.1, {"unit": "Hz"}),
        ("battery_i", "clamp_mock_lcd", "Battery current (clamp)", "current", "A", 0.1, {"unit": "A"}),
    ],
}
FRAME_SIZE = {"w": 1400, "h": 820}

# Test meters for third-party footage in data/footage (TESTING ONLY). No regions: SAM 3.1 must find the display.
METER_INSTRUMENTS = [
    # role, label, quantity, unit, resolution, match
    ("energy_kwh", "Energy", "energy", "kWh", 0.01, {"unit": "kWh"}),
    ("voltage_v", "Voltage", "voltage", "V", 0.1, {"unit": "V"}),
    ("current_a", "Current", "current", "A", 0.001, {"unit": "A"}),
    ("power_kw", "Active power", "power", "kW", 0.001, {"unit": "kW"}),
    ("frequency_hz", "Frequency", "frequency", "Hz", 0.01, {"unit": "Hz"}),
]
TEST_METERS = [("meter_test1", "Smart energy meter (test footage)"), ("meter_test2", "kWh energy meter (test footage)")]


# Real footage from a friend in Germany (with permission): two electricity meters and a water meter.
REAL = {
    "site": ("friend-de", "Friend's meters (Germany)", "Germany"),
    "equipment": [
        ("de_latronic", "Latronic L20-1Z1-20A0 bidirectional meter, 3x230/400 V, 5(60) A",
         "German bidirectional electricity meter (Zweirichtungszaehler) LCD. It cycles OBIS registers: 1.8.0 = energy "
         "imported (Bezug), 2.8.0 = energy exported (Lieferung); the OBIS code is shown small on the left.",
         [("energy_kwh", "Energy imported (1.8.0)", "energy", "kWh", 1, {"unit": "kWh", "page": ["1.8.0"], "label": ["1.8.0", "bezug", "import"]}),
          ("energy_export_kwh", "Energy exported (2.8.0)", "energy", "kWh", 1, {"unit": "kWh", "page": ["2.8.0"], "label": ["2.8.0", "lieferung", "export"]})]),
        ("de_dzg", "DZG WS7412.1T.G2 single-phase meter, 230 V, 5(60) A",
         "German single-phase electricity meter LCD (DZG). OBIS 1.8.0 = total energy imported; it may also show tariff "
         "registers 1.8.1 / 1.8.2.",
         [("energy_kwh", "Energy imported (1.8.0)", "energy", "kWh", 1, {"unit": "kWh", "page": ["1.8.0"], "label": ["1.8.0"], "strict": True})]),
        ("de_metrona", "Metrona water meter, Q3 2.5 m3/h",
         "German water meter LCD (Metrona). Volume in m3 with a DECIMAL COMMA: '2,388' means 2.388 m3. Unlit ghost "
         "8s around the digits are not digits.",
         [("volume_m3", "Water volume", "volume", "m3", 0.001, {"unit": "m3"})]),
    ],
}


async def seed_real(database_url: str) -> None:
    pool = await create_pool(database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            site_id, name, location = REAL["site"]
            await conn.execute("""INSERT INTO sites (site_id, name, location) VALUES ($1, $2, $3)
                                  ON CONFLICT (site_id) DO UPDATE SET name = $2, location = $3""", site_id, name, location)
            for eq_id, model, context, instruments in REAL["equipment"]:
                await conn.execute(
                    """INSERT INTO equipment (equipment_id, site_id, kind, model) VALUES ($1, $2, 'meter', $3)
                       ON CONFLICT (equipment_id) DO UPDATE SET model = $3""", eq_id, site_id, model)
                display_id = f"{eq_id}_lcd"
                await conn.execute(
                    """INSERT INTO displays (display_id, equipment_id, kind, label, roi, roi_frame_size, context,
                                             sam_prompt, layout_rank)
                       VALUES ($1, $2, 'digital', 'Meter LCD', NULL, NULL, $3, 'digital display', 0)
                       ON CONFLICT (display_id) DO UPDATE SET context = $3""", display_id, eq_id, context)
                for role, label, quantity, unit, resolution, match in instruments:
                    await conn.execute(
                        """INSERT INTO instruments (instrument_id, equipment_id, kind, label, quantity, unit, physics_role,
                                                    resolution, display_id, match)
                           VALUES ($1, $2, 'digital', $3, $4, $5, $6, $7, $8, $9)
                           ON CONFLICT (instrument_id) DO UPDATE SET label = $3, unit = $5, resolution = $7, match = $9""",
                        f"{eq_id}_{role}", eq_id, label, quantity, unit, role, resolution, display_id, match)
    finally:
        await pool.close()


async def seed_meters(database_url: str) -> None:
    pool = await create_pool(database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("""INSERT INTO sites (site_id, name, location) VALUES ('test-footage', 'Test footage', NULL)
                                  ON CONFLICT (site_id) DO NOTHING""")
            for eq_id, model in TEST_METERS:
                await conn.execute(
                    """INSERT INTO equipment (equipment_id, site_id, kind, model) VALUES ($1, 'test-footage', 'meter', $2)
                       ON CONFLICT (equipment_id) DO UPDATE SET model = $2""", eq_id, model)
                display_id = f"{eq_id}_lcd"
                await conn.execute(
                    """INSERT INTO displays (display_id, equipment_id, kind, label, roi, roi_frame_size, context,
                                             sam_prompt, layout_rank)
                       VALUES ($1, $2, 'digital', 'Meter LCD', NULL, NULL, 'electricity energy meter display',
                               'digital display', 0)
                       ON CONFLICT (display_id) DO UPDATE SET roi = NULL, context = 'electricity energy meter display'""",
                    display_id, eq_id)
                for role, label, quantity, unit, resolution, match in METER_INSTRUMENTS:
                    await conn.execute(
                        """INSERT INTO instruments (instrument_id, equipment_id, kind, label, quantity, unit, physics_role,
                                                    resolution, display_id, match)
                           VALUES ($1, $2, 'digital', $3, $4, $5, $6, $7, $8, $9)
                           ON CONFLICT (instrument_id) DO UPDATE SET match = $9, resolution = $7""",
                        f"{eq_id}_{role}", eq_id, label, quantity, unit, role, resolution, display_id, match)
    finally:
        await pool.close()


async def seed_mock(database_url: str) -> None:
    pool = await create_pool(database_url)
    try:
        async with pool.acquire() as conn, conn.transaction():
            site_id, name, location = MOCK["site"]
            await conn.execute("""INSERT INTO sites (site_id, name, location) VALUES ($1, $2, $3)
                                  ON CONFLICT (site_id) DO UPDATE SET name = $2, location = $3""", site_id, name, location)
            for eq_id, kind, model, nominal, rated, idle, linked in MOCK["equipment"]:
                await conn.execute(
                    """INSERT INTO equipment (equipment_id, site_id, kind, model, nominal_battery_v, rated_va, idle_w,
                                              linked_equipment_id)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                       ON CONFLICT (equipment_id) DO UPDATE SET kind = $3, model = $4, nominal_battery_v = $5,
                           rated_va = $6, idle_w = $7, linked_equipment_id = $8""",
                    eq_id, site_id, kind, model, nominal, rated, idle, linked)
            for rank, (display_id, eq_id, label, roi, context) in enumerate(MOCK["displays"]):
                await conn.execute(
                    """INSERT INTO displays (display_id, equipment_id, kind, label, roi, roi_frame_size, context,
                                             sam_prompt, layout_rank)
                       VALUES ($1, $2, 'digital', $3, $4, $5, $6, 'digital display', $7)
                       ON CONFLICT (display_id) DO UPDATE SET label = $3, roi = $4, roi_frame_size = $5, context = $6,
                           sam_prompt = 'digital display', layout_rank = $7""",
                    display_id, eq_id, label, roi, FRAME_SIZE, context, rank)       # inverter left, clamp right
            equipment_of = {d[0]: d[1] for d in MOCK["displays"]}
            for role, display_id, label, quantity, unit, resolution, match in MOCK["instruments"]:
                eq_id = equipment_of[display_id]
                await conn.execute(
                    """INSERT INTO instruments (instrument_id, equipment_id, kind, label, quantity, unit, physics_role,
                                                resolution, display_id, match)
                       VALUES ($1, $2, 'digital', $3, $4, $5, $6, $7, $8, $9)
                       ON CONFLICT (instrument_id) DO UPDATE SET label = $3, quantity = $4, unit = $5,
                           physics_role = $6, resolution = $7, display_id = $8, match = $9""",
                    f"{eq_id}_{role}", eq_id, label, quantity, unit, role, resolution, display_id, match)
    finally:
        await pool.close()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("mock", "meters", "real"):
        print("usage: python -m backend.app.seed mock|meters|real")
        return 2
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set.")
        return 1
    if sys.argv[1] == "real":
        asyncio.run(seed_real(settings.database_url))
        print("Seeded the real meters: de_latronic (import/export), de_dzg, de_metrona (water).")
        return 0
    if sys.argv[1] == "meters":
        asyncio.run(seed_meters(settings.database_url))
        print("Seeded test meters: meter_test1, meter_test2 (no regions: SAM 3.1 finds the display).")
        return 0
    asyncio.run(seed_mock(settings.database_url))
    print("Seeded the mock panel: inv_mock (inverter LCD, 7 instruments) + clamp_mock (DC clamp meter).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
