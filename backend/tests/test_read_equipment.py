import asyncio
import io

from PIL import Image

from backend.app.llm import ChatClient
from backend.app.pipeline.read_equipment import (TOOL_NAME, EquipmentInventory, parse_power_kva, parse_ratio,
                                                 parse_ratios, read_inventory, suggest_registry)
from backend.tests.fakes import FakeRawClient, make_settings, tool_response

INVENTORY = {
    "scene": "nameplate", "privacy_flags": ["serial number on the plate"], "notes": None,
    "items": [
        {"kind": "distribution_transformer", "label": "T1", "readings": [], "notes": None, "nameplate": [
            {"name": "Rated power", "value": "500", "unit": "kVA", "legible": True},
            {"name": "Rated voltage", "value": "11/0.415", "unit": "kV", "legible": True},
            {"name": "Vector group", "value": "Dyn11", "unit": None, "legible": True},
            {"name": "Impedance voltage", "value": "4.5", "unit": "%", "legible": True},
            {"name": "Serial", "value": "12?45", "unit": None, "legible": False}]},
        {"kind": "current_transformer", "label": "R-phase CT", "readings": [], "notes": None, "nameplate": [
            {"name": "Ratio", "value": "400/5", "unit": "A", "legible": True},
            {"name": "Class", "value": "5P20", "unit": None, "legible": True}]},
        {"kind": "multifunction_meter", "label": None, "notes": None, "nameplate": [], "readings": [
            {"label": "VLL", "value": 413.0, "unit": "V", "raw_text": "413", "legible": True}]},
    ],
}


def test_ratios_and_ratings_are_parsed_deterministically():
    assert parse_ratio("400/5", "A") == (400, 5)
    assert parse_ratio("11/0.415", "kV") == (11000, 415)
    assert parse_ratio("11kV/110V") == (11000, 110)
    assert parse_ratio("2000 / 1", "A") == (2000, 1)
    assert parse_ratio("630/1-1-1A", "A") == (630, 1)                       # Siemens multi-core notation
    assert parse_ratio("(220 kV / √3) : (110 V / √3)") == (220000, 110)      # VT ratio with √3 on both sides
    assert parse_ratios("200/1 400/1 600/1") == [(200, 1), (400, 1), (600, 1)]
    assert parse_ratio("Dyn11") is None and parse_ratio("0/5") is None
    assert parse_power_kva("500", "kVA") == 500 and parse_power_kva("2.5", "MVA") == 2500
    assert parse_power_kva("500", "kW") is None
    assert parse_power_kva("30", "VA") is None                               # a burden, not a kVA rating


def plate(kind, *fields, scene="nameplate"):
    return EquipmentInventory.model_validate({"scene": scene, "privacy_flags": [], "items": [
        {"kind": kind, "readings": [], "nameplate": [{"name": n, "value": v, "unit": u, "legible": True}
                                                     for n, v, u in fields]}]})


def test_real_plates_from_the_oct_26_photo_test_give_correct_suggestions():
    # Regression cases transcribed by Muse Spark from real nameplate photos (docs/spike_results.md).
    siemens = plate("current_transformer", ("Um", "12/28/75", "kV"), ("CT ratio", "630/1-1-1A", "A"),
                    ("Date", "06 / 24", None), ("RCT", "≤ 6 ohm AT 75'C", None), ("Ith", "40kA/3s", None))
    assert suggest_registry(siemens)[0]["params"] == {"ct_ratio": {"primary": 630, "secondary": 1, "factor": 630}}

    multi_tap = plate("current_transformer", ("Um", "69/115", "kV"), ("Utest", "230 / 550", "kV"),
                      ("Ratio 1S1-1S3", "1000/5", None), ("Ratio 1S1-1S2", "500/5", None),
                      ("Ratio 2S1-2S3", "1000/5", None), ("Ratio 4S1-4S2", "2000/5", None))
    taps = suggest_registry(multi_tap)[0]["params"]["ct_ratio_taps"]
    assert [t["factor"] for t in taps] == [200, 100, 400]

    generic_400kv = plate("current_transformer", ("Transformation Ratio", "2000 / 1", "A"),
                          ("Core - 2 (Protection) Rated Burden", "30", "VA"), ("Rated Frequency", "50", "Hz"))
    assert suggest_registry(generic_400kv)[0]["params"] == {"ct_ratio": {"primary": 2000, "secondary": 1, "factor": 2000}}

    kenya = plate("current_transformer", ("CORE NO. I RATIO", "200/1 400/1 600/1", None),
                  ("CORE NO. III RATIO", "600/1 200/1", None), ("TYPE", "CT : 72.5 / 140 / 325", None))
    assert [t["primary"] for t in suggest_registry(kenya)[0]["params"]["ct_ratio_taps"]] == [200, 400, 600]

    infographic = plate("voltage_transformer", ("VT Ratio", "(220 kV / √3) : (110 V / √3)", None), scene="other")
    assert suggest_registry(infographic) == []                               # a calculation sheet, not equipment


def test_registry_suggestions_come_only_from_legible_nameplate_text():
    suggestions = suggest_registry(EquipmentInventory.model_validate(INVENTORY))
    transformer, ct = suggestions
    assert transformer["params"]["rated_kva"] == 500
    assert transformer["params"]["voltage_ratio"]["factor"] == round(11000 / 415, 6)
    assert transformer["params"]["vector_group"] == "Dyn11" and transformer["params"]["impedance_pct"] == 4.5
    assert ct["label"] == "R-phase CT" and ct["params"]["ct_ratio"]["factor"] == 80
    assert len(suggestions) == 2                       # the meter has readings but no nameplate parameters


def test_read_inventory_sends_the_photo_and_validates(tmp_path):
    out = io.BytesIO()
    Image.new("RGB", (900, 600), (90, 90, 90)).save(out, format="PNG")
    raw = FakeRawClient([tool_response(INVENTORY, name=TOOL_NAME)])
    inventory, meta = asyncio.run(read_inventory(ChatClient(make_settings(tmp_path), raw_client=raw), out.getvalue()))
    assert inventory.items[1].kind == "current_transformer" and meta.prompt_version == "inventory_v1"
    assert raw.completions.calls[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png")
