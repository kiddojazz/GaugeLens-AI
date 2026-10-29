"""Which reading is wrong? Digit-error candidate search, then confidence / jump / age ranking.
The candidate only chooses what to re-look at; it is never written as a reading."""

from backend.app.physics.engine import attribute, evaluate
from backend.app.physics.rules import Rule, RulePack, load_pack
from backend.app.physics.state import RoleReading, RoleState
from backend.tests.test_physics import CLEAN, INVERTER, state_with


def run(values, pack=None, **kw):
    pack = pack or load_pack("inverter")
    state = state_with(values, **kw)
    results = evaluate(pack, state, INVERTER, now=100.0)
    return attribute(pack, state, INVERTER, results, now=100.0)


def test_doubled_battery_voltage_is_found_with_its_fix():
    a = run({**CLEAN, "battery_v": 50.0})
    assert a.suspect_role == "battery_v" and a.suspect_reading_id == "rd_battery_v"
    assert a.candidate_fix == {"role": "battery_v", "reading_id": "rd_battery_v", "value": 25.0, "kind": "div2"}


def test_seven_segment_3_read_as_8_is_found():
    a = run({**CLEAN, "output_w": 838})
    assert a.suspect_role == "output_w" and a.candidate_fix["value"] == 338 and a.candidate_fix["kind"] == "seg_8to3"


def test_dropped_decimal_on_the_clamp_meter_is_found():
    a = run({**CLEAN, "battery_i": 161.0})
    assert a.suspect_role == "battery_i" and a.candidate_fix["value"] == 16.1 and a.candidate_fix["kind"] == "div10"


def test_seven_read_as_nine_at_low_load_is_not_caught_by_physics():
    # A documented LIMITATION (Spike B failure mode): the clamp's 17.1 A read as 19.1 A is +12 %. At 300 W
    # the inverter is below 15 % load, so the tolerance is 30 % and physics alone can't see it.
    # Two-frame agreement and the seven-segment decoder must catch this class (docs/spike_results.md).
    values = {**CLEAN, "battery_i": 19.1, "battery_v": 25.0, "output_w": 300, "output_va": 349, "load_pct": 10}
    results = {r.rule_id: r for r in evaluate(load_pack("inverter"), state_with(values), INVERTER, now=100.0)}
    assert results["inverter.dc_power_balance"].status == "pass" and results["inverter.dc_power_balance"].low_load
    assert run(values).suspect_reading_id is None


def test_several_values_fitting_one_reading_are_reported_as_ambiguous():
    # Only b can explain the failure, but 9.0 (b/2) and 10.0 (seg 8->0) both fit within 15 %.
    pack = RulePack(equipment_kind="test", prefix="t.",
                    rules=[Rule(id="t.k", inputs=["b"], expected="k", observed="b", tolerance_pct=15,
                                requires_steady=False)])
    state = RoleState()
    state.update(RoleReading(role="b", value=18.0, ts=100.0, reading_id="rd_b", raw_text="18.0"))
    a = attribute(pack, state, {"k": 10}, evaluate(pack, state, {"k": 10}, now=100.0), now=100.0)
    assert a.suspect_reading_id == "rd_b" and a.candidate_fix is None and "ambiguous value" in a.reason


def test_ambiguous_fixes_name_a_suspect_but_no_candidate():
    # With only the power-balance rule, halving V or halving I fixes it equally: no single explanation.
    pack = RulePack(equipment_kind="inverter", prefix="t.", rules=[r for r in load_pack("inverter").rules
                                                                 if r.id == "inverter.dc_power_balance"])
    a = run({**CLEAN, "battery_i": 32.2}, pack=pack)
    assert a.candidate_fix is None and a.suspect_reading_id is not None
    assert "ambiguous" in a.reason


def test_without_any_fix_the_lowest_confidence_reading_is_suspected():
    # a = 10.0 vs b = 13.7 within 1 %: no x2 / x10 / seven-segment variant of either value closes the gap
    pack = RulePack(equipment_kind="test", prefix="t.",
                    rules=[Rule(id="t.equal", inputs=["a", "b"], expected="a", observed="b", tolerance_pct=1,
                                requires_steady=False)])
    state = RoleState()
    state.update(RoleReading(role="a", value=10.0, ts=100.0, reading_id="rd_a", confidence=0.9, raw_text="10.0"))
    state.update(RoleReading(role="b", value=13.7, ts=100.0, reading_id="rd_b", confidence=0.4, raw_text="13.7"))
    results = evaluate(pack, state, {}, now=100.0)
    a = attribute(pack, state, {}, results, now=100.0)
    assert a.candidate_fix is None and a.suspect_reading_id == "rd_b" and "no single" in a.reason


def test_nothing_failing_means_nothing_to_attribute():
    assert run(CLEAN).suspect_reading_id is None


def test_rule_validation_requires_exactly_one_form():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Rule(id="x", inputs=["a"], check="a > 0", expected="a", observed="a")
    with pytest.raises(ValidationError):
        Rule(id="x", inputs=["a"])
    assert isinstance(RoleState(), RoleState)


def test_misread_variants_respect_a_decimal_comma():
    from backend.app.physics.engine import _number_text, misread_variants
    from backend.app.physics.state import RoleReading

    assert _number_text("2,388", 2.388) == "2.388"            # German water meter: decimal comma
    assert _number_text("1,234.5", 1234.5) == "1234.5"        # thousands comma
    assert _number_text("1.234,5", 1234.5) == "1234.5"        # thousands dot, decimal comma
    assert _number_text("002973", 2973.0) == "002973"
    variants = dict((kind, v) for v, kind in misread_variants(RoleReading("volume_m3", 2.388, 0.0, raw_text="2,388")))
    assert variants["seg_3to8"] == 2.888                      # a 3 misread for an 8 stays in m3, not 2888
