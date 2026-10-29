"""Physics rule packs on hand-computed values (docs/04_physics_rules_spec.md §8).

Inverter used throughout: 24 V system, 3.5 kVA, idle 35 W, efficiency band 0.85-0.95.
Clean state: 338 W out -> P_dc = 338 / 0.92 + 35 = 402 W -> at 25.0 V the battery current is ~16.1 A.
"""

import pytest

from backend.app.physics.engine import evaluate
from backend.app.physics.rules import load_pack
from backend.app.physics.state import RoleReading, RoleState

INVERTER = {"nominal_v": 24, "rated_va": 3500, "idle_w": 35}


def state_with(values: dict, t: float = 100.0, resolution: dict | None = None, confidence: float = 0.9) -> RoleState:
    state = RoleState(steady_role="output_w")
    for role, value in values.items():
        state.update(RoleReading(role=role, value=value, ts=t, reading_id=f"rd_{role}", confidence=confidence,
                                 resolution=(resolution or {}).get(role), raw_text=str(value)))
    return state


CLEAN = {"battery_v": 25.0, "battery_i": 16.1, "output_w": 338, "output_va": 393, "load_pct": 11,
         "ac_v": 230, "ac_hz": 50.0}


def by_id(results):
    return {r.rule_id: r for r in results}


def test_clean_inverter_readings_pass_every_rule_that_can_run():
    results = by_id(evaluate(load_pack("inverter"), state_with(CLEAN), INVERTER, now=100.0))
    for rule_id in ("inverter.dc_power_balance", "inverter.load_pct_vs_va", "inverter.power_factor_bound",
                    "inverter.battery_voltage_band", "inverter.output_frequency", "inverter.output_voltage"):
        assert results[rule_id].status == "pass", (rule_id, results[rule_id].reason)
    assert results["inverter.apparent_power"].status == "skipped"          # no AC current on this display
    assert results["inverter.dc_power_balance"].residual_pct == 0.0     # observed falls inside the efficiency band


@pytest.mark.parametrize("role,bad,failing", [
    ("battery_v", 250.0, "inverter.dc_power_balance"),     # dropped decimal: x10
    ("battery_v", 50.0, "inverter.dc_power_balance"),      # doubled
    ("battery_i", 161.0, "inverter.dc_power_balance"),     # dropped decimal on the clamp meter
    ("output_w", 838, "inverter.dc_power_balance"),        # seven-segment 3 -> 8
    ("output_w", 838, "inverter.power_factor_bound"),      # W can't exceed VA
    ("battery_v", 50.0, "inverter.battery_voltage_band"),
    ("ac_hz", 5.0, "inverter.output_frequency"),           # dropped decimal
])
def test_gross_misreads_break_physics(role, bad, failing):
    results = by_id(evaluate(load_pack("inverter"), state_with({**CLEAN, role: bad}), INVERTER, now=100.0))
    assert results[failing].status == "fail"
    assert results[failing].residual_pct is None or abs(results[failing].residual_pct) > results[failing].tolerance_used


def test_small_errors_inside_tolerance_are_not_caught_by_design():
    results = by_id(evaluate(load_pack("inverter"), state_with({**CLEAN, "battery_v": 25.5}), INVERTER, now=100.0))
    assert results["inverter.dc_power_balance"].status == "pass"


def test_stale_or_missing_inputs_skip_rules_instead_of_guessing():
    state = state_with(CLEAN, t=100.0)
    state.update(RoleReading(role="battery_v", value=25.0, ts=60.0, reading_id="old"))   # 40 s old at now=100
    results = by_id(evaluate(load_pack("inverter"), state, INVERTER, now=100.0))
    assert results["inverter.dc_power_balance"].status == "skipped"
    assert "stale" in results["inverter.dc_power_balance"].reason

    no_clamp = {k: v for k, v in CLEAN.items() if k != "battery_i"}
    results = by_id(evaluate(load_pack("inverter"), state_with(no_clamp), INVERTER, now=100.0))
    assert results["inverter.dc_power_balance"].status == "skipped"
    assert "battery_i" in results["inverter.dc_power_balance"].reason


def test_load_steps_pause_steady_state_rules_but_not_band_rules():
    state = state_with(CLEAN, t=100.0)
    state.update(RoleReading(role="output_w", value=1838, ts=101.0, reading_id="kettle"))  # kettle on: +1500 W
    results = by_id(evaluate(load_pack("inverter"), state, INVERTER, now=102.0))
    assert results["inverter.dc_power_balance"].status == "skipped" and "steady" in results["inverter.dc_power_balance"].reason
    assert results["inverter.battery_voltage_band"].status == "pass"          # requires_steady: false

    # 9 s later, a full fresh set (the battery sags to 24.4 V under the kettle): steady again, physics holds
    for role, value in {"battery_v": 24.4, "battery_i": (1838 / 0.92 + 35) / 24.4, "output_va": 1838 / 0.97,
                        "load_pct": 54, "output_w": 1838}.items():
        state.update(RoleReading(role=role, value=value, ts=110.0, reading_id=f"after_{role}"))
    results = by_id(evaluate(load_pack("inverter"), state, INVERTER, now=110.0))
    assert results["inverter.dc_power_balance"].status == "pass"

    # mixing the pre-step battery voltage with post-step power is physically invalid: keep skipping
    state.update(RoleReading(role="battery_v", value=25.0, ts=100.0, reading_id="pre_step_v"))
    results = by_id(evaluate(load_pack("inverter"), state, INVERTER, now=110.0))
    assert results["inverter.dc_power_balance"].status == "skipped"


def test_a_misread_that_looks_like_a_load_step_is_still_caught_from_the_same_frame():
    # Found in the coherent mock replay: output 1711 W misread as 7711 W looked like a load step and paused
    # every steady-state rule. W and VA come from the same frame, so their check must still run.
    state = state_with({"output_w": 1711, "output_va": 1764, "load_pct": 50}, t=1.5)
    for role, value in {"output_w": 7711, "output_va": 1764, "load_pct": 50}.items():
        state.update(RoleReading(role=role, value=value, ts=6.0, reading_id=f"rd_{role}", raw_text=str(value)))
    results = by_id(evaluate(load_pack("inverter"), state, INVERTER, now=6.0))
    assert results["inverter.power_factor_bound"].status == "fail"
    assert results["inverter.load_pct_vs_va"].status == "pass"


def test_power_factor_floor_rejects_implausible_watts():
    results = by_id(evaluate(load_pack("inverter"), state_with({**CLEAN, "output_w": 171, "output_va": 1764,
                                                                "load_pct": 50}), INVERTER, now=100.0))
    assert results["inverter.power_factor_floor"].status == "fail"            # PF 0.1 under real load
    idle = by_id(evaluate(load_pack("inverter"), state_with({**CLEAN, "output_w": 12, "output_va": 60,
                                                             "load_pct": 2}), INVERTER, now=100.0))
    assert idle["inverter.power_factor_floor"].status == "skipped"            # near idle PF means little


def test_low_load_widens_the_tolerance():
    # 100 W out is below 15 % of 3.5 kVA: idle loss dominates and the efficiency band is unreliable
    values = {**CLEAN, "output_w": 100, "output_va": 117, "load_pct": 3, "battery_i": 6.2}
    result = by_id(evaluate(load_pack("inverter"), state_with(values), INVERTER, now=100.0))["inverter.dc_power_balance"]
    assert result.tolerance_used == 30 and result.low_load and result.status == "pass"


def test_display_rounding_raises_the_effective_tolerance():
    # load shown as an integer percent: at 2 % a half-step is 25 % of the value
    values = {**CLEAN, "output_w": 60, "output_va": 70, "load_pct": 2}
    result = by_id(evaluate(load_pack("inverter"), state_with(values, resolution={"load_pct": 1}), INVERTER,
                            now=100.0))["inverter.load_pct_vs_va"]
    assert result.tolerance_used >= 25 and result.status == "pass"


def test_mppt_and_pwm_packs_follow_their_physics():
    mppt = by_id(evaluate(load_pack("cc_mppt"), state_with({"pv_v": 80.0, "pv_i": 5.0, "pv_w": 400, "battery_v": 26.0,
                                                             "charge_i": 14.6}), {}, now=100.0))
    assert {r.status for r in mppt.values()} == {"pass"}
    impossible = by_id(evaluate(load_pack("cc_mppt"), state_with({"pv_v": 80.0, "pv_i": 5.0, "battery_v": 26.0,
                                                                  "charge_i": 18.0}), {}, now=100.0))
    assert impossible["cc_mppt.power_balance"].status == "fail"                # more power out than in

    pwm = by_id(evaluate(load_pack("cc_pwm"), state_with({"pv_v": 27.1, "pv_i": 8.0, "battery_v": 26.0,
                                                           "charge_i": 7.8}), {}, now=100.0))
    assert {r.status for r in pwm.values()} == {"pass"}


def test_generator_droop_is_a_transition_rule():
    pack = load_pack("generator")
    state = RoleState(steady_role="ac_i")
    for role, value in {"hz": 51.8, "ac_v": 231, "ac_i": 4.0}.items():
        state.update(RoleReading(role=role, value=value, ts=0.0, reading_id=f"a_{role}"))
    for role, value in {"hz": 52.4, "ac_v": 229, "ac_i": 12.0}.items():      # load up, frequency UP: impossible
        state.update(RoleReading(role=role, value=value, ts=20.0, reading_id=f"b_{role}"))
    results = by_id(evaluate(pack, state, {}, now=20.0))
    assert results["generator.governor_droop"].status == "fail"
    assert results["generator.freq_band"].status == "pass"

    state.update(RoleReading(role="hz", value=51.2, ts=30.0, reading_id="c_hz"))   # frequency sags: normal droop
    state.update(RoleReading(role="ac_i", value=13.0, ts=30.0, reading_id="c_ac_i"))
    assert by_id(evaluate(pack, state, {}, now=30.0))["generator.governor_droop"].status == "pass"


def test_every_shipped_pack_loads_and_validates():
    for name in ("inverter", "cc_mppt", "cc_pwm", "generator", "pressure"):
        pack = load_pack(name)
        assert pack.rules and all(r.id.startswith(pack.prefix) for r in pack.rules)
