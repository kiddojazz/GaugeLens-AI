import numpy as np

from backend.app.pipeline.ingest import changed, crop_signature, sample_frames, scale_roi
from backend.app.pipeline.mapping import map_fields
from backend.app.pipeline.read_digital import DisplayField, DisplayReading
from backend.app.seed import MOCK
from backend.tests.videos import solid_states_video

INSTRUMENTS = [{"instrument_id": f"inv_{role}", "unit": unit, "match": match}
               for role, display, _, _, unit, _, match in MOCK["instruments"] if display == "inv_mock_lcd"]


def reading(page, *fields):
    return DisplayReading(display_kind="seven_segment", page_hint=page, glare_or_occlusion=False,
                          fields=[DisplayField(label=label, value=value, unit=unit, raw_text=str(value), legible=True)
                                  for label, value, unit in fields])


def mapped(result):
    return {i["instrument_id"]: f.value for i, f in result.mapped}


def test_battery_page_maps_volts_and_percent_to_battery_roles():
    result = map_fields(reading("battery", ("BATT", 23.4, "V"), ("BATT", 30, "%")), INSTRUMENTS)
    assert mapped(result) == {"inv_battery_v": 23.4, "inv_battery_soc_pct": 30} and not result.unmapped


def test_same_units_on_other_pages_map_to_other_roles():
    assert mapped(map_fields(reading("AC output", ("AC OUT", 229, "V"), ("AC OUT", 50.0, "Hz")), INSTRUMENTS)) == \
        {"inv_ac_v": 229, "inv_ac_hz": 50.0}
    assert mapped(map_fields(reading("output", ("OUTPUT", 338, "W"), ("OUTPUT", 393, "VA"), ("LOAD", 11, "%")),
                             INSTRUMENTS)) == {"inv_output_w": 338, "inv_output_va": 393, "inv_load_pct": 11}


def test_ambiguous_or_unknown_fields_stay_unmapped_never_guessed():
    result = map_fields(reading(None, ("", 24.0, "V"), ("", 3.0, "kWh")), INSTRUMENTS)
    assert result.mapped == [] and [f.unit for f in result.unmapped] == ["V", "kWh"]


def test_unit_spacing_and_case_do_not_matter():
    meter = [{"instrument_id": "kwh", "unit": "kWh", "match": {"unit": "kWh"}}]
    for printed in ("kW h", "KWH", "kWh", " kwh "):
        assert mapped(map_fields(reading("energy", ("Energy", 9440, printed)), meter)) == {"kwh": 9440}


def test_an_instrument_is_used_once_per_reading():
    result = map_fields(reading("output", ("OUTPUT", 338, "W"), ("OUTPUT", 339, "W")), INSTRUMENTS)
    assert mapped(result) == {"inv_output_w": 338} and len(result.unmapped) == 1


def _real(eq_id):
    from backend.app.seed import REAL
    _, _, _, instruments = next(e for e in REAL["equipment"] if e[0] == eq_id)
    return [{"instrument_id": f"{eq_id}_{role}", "physics_role": role, "unit": unit, "match": match}
            for role, _, _, unit, _, match in instruments]


def test_meter_registers_map_by_obis_code_and_unreadable_codes_stay_unmapped():
    latronic = _real("de_latronic")
    assert mapped(map_fields(reading("1.8.0", ("energy", 2320, "kWh")), latronic)) == {"de_latronic_energy_kwh": 2320}
    assert mapped(map_fields(reading("2.8.0", ("energy", 0, "kWh")), latronic)) == {"de_latronic_energy_export_kwh": 0}
    unknown = map_fields(reading(None, ("energy register (OBIS code not legible)", 0, "kWh")), latronic)
    assert unknown.mapped == [] and len(unknown.unmapped) == 1             # import or export? never guessed


def test_a_strict_register_ignores_tariff_registers_and_m3_matches_m_cubed():
    dzg = _real("de_dzg")
    assert mapped(map_fields(reading("1.8.0", ("1.8.0", 2973, "kWh")), dzg)) == {"de_dzg_energy_kwh": 2973}
    assert map_fields(reading("1.8.1", ("1.8.1", 1900, "kWh")), dzg).mapped == []
    assert mapped(map_fields(reading(None, ("volume", 2.388, "m³")), _real("de_metrona"))) == {"de_metrona_volume_m3": 2.388}
    assert mapped(map_fields(reading(None, ("Volume", 2.388, None)), _real("de_metrona"))) == {"de_metrona_volume_m3": 2.388}
    assert map_fields(reading(None, ("energy", 0, None)), _real("de_latronic")).mapped == []   # two kWh registers: no


def test_frames_are_sampled_by_timestamp(tmp_path):
    video = solid_states_video(tmp_path / "v.mp4", [(0, 0, 0), (255, 255, 255), (0, 0, 255)], seconds_each=1.0)
    frames = sample_frames(str(video), fps=2.0)
    assert [f.t_offset_s for f in frames] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert frames[0].size == (1400, 820) and frames[0].jpeg[:2] == b"\xff\xd8"


def test_change_detection_sees_state_changes_only(tmp_path):
    frames = sample_frames(str(solid_states_video(tmp_path / "v.mp4", [(20, 20, 20), (200, 200, 200)])), fps=2.0)
    box = (245, 238, 640, 255)
    sig = [crop_signature(f.jpeg, box) for f in frames]
    assert changed(None, sig[0], 3.0)
    assert not changed(sig[0], sig[1], 3.0)            # same state, half a second later
    assert changed(sig[1], sig[2], 3.0)                # new state
    assert isinstance(sig[0], np.ndarray) and sig[0].shape[1] == 160


def test_a_page_change_that_moves_only_a_few_digits_is_detected():
    # Regression (coherent mock clip, Oct 27): "230 V / 50.0 Hz" -> "23.6 V / 93 %" changed the whole-crop
    # mean by only 2.6 and was missed. A local change must count even when most of the LCD is still.
    still = np.full((64, 160), 150.0, dtype=np.float32)
    few_digits = still.copy()
    few_digits[20:32, 100:112] = 40.0                       # one small digit cell goes dark
    assert float(np.mean(np.abs(few_digits - still))) < 3.0  # the old metric would have said "unchanged"
    assert changed(still, few_digits, 8.0)
    noise = still + np.random.default_rng(0).normal(0, 1.0, still.shape).astype(np.float32)
    assert not changed(still, noise, 8.0)                   # sensor / JPEG noise stays below threshold


def test_rois_scale_to_the_clip_resolution():
    assert scale_roi({"x": 245, "y": 238, "w": 640, "h": 255}, {"w": 1400, "h": 820}, (700, 410)) == (122, 119, 320, 128)
    assert scale_roi({"x": 10, "y": 20, "w": 30, "h": 40}, None, (1920, 1080)) == (10, 20, 30, 40)
