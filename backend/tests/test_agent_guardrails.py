from backend.app.agent.guardrails import _supported, citation_problems, template_diagnosis, unsafe_in
from backend.app.agent.schemas import DiagnosisOutput

EVIDENCE = """
- check:chk_1: inverter.power_factor_bound persistent_violation; expected None, observed None
{"checks": [{"id": "check:chk_1", "rule_id": "inverter.power_factor_bound", "residual_pct": null},
            {"id": "check:chk_2", "rule_id": "inverter.dc_power_balance", "residual_pct": 41.2}]}
{"role": "output_w", "readings": [{"id": "reading:rd_w", "t": 1.5, "value": 838, "raw_text": "838"}]}
{"frame": "frame:clip_f00003"}
"""
CHECKS = {"chk_1": {"rule_id": "inverter.power_factor_bound", "residual_pct": None},
          "chk_2": {"rule_id": "inverter.dc_power_balance", "residual_pct": 41.2}}


def diagnosis(**overrides) -> DiagnosisOutput:
    base = {
        "headline": "Output power exceeds apparent power",
        "observations": [{"statement": "Output power read 838 W at t=1.5 s", "evidence": ["reading:rd_w", "frame:clip_f00003"]}],
        "physics": [{"rule_id": "inverter.dc_power_balance", "result": "persistent_violation", "residual_pct": 41.2,
                     "evidence": ["check:chk_2"]}],
        "hypotheses": [{"cause": "Load with unusual power factor", "likelihood": "low", "supporting": ["check:chk_1"],
                        "against": []}],
        "unknowns": ["No AC current is displayed"],
        "proposed_action": "Measure AC output current with a clamp meter under the same load.",
        "safety_note": "Do not open the inverter enclosure.", "confidence": "medium"}
    base.update(overrides)
    return DiagnosisOutput.model_validate(base)


def test_a_grounded_diagnosis_passes():
    assert citation_problems(diagnosis(), EVIDENCE, CHECKS) == []


def test_invented_ids_and_numbers_are_caught():
    bad = diagnosis(observations=[{"statement": "Battery fell 1.5 V to 22.9 V", "evidence": ["reading:rd_made_up"]}])
    problems = citation_problems(bad, EVIDENCE, CHECKS)
    assert "unknown evidence id reading:rd_made_up" in problems and "number 22.9 not in any tool result" in problems


def test_physics_must_match_the_stored_check():
    wrong_residual = diagnosis(physics=[{"rule_id": "inverter.dc_power_balance", "result": "persistent_violation",
                                         "residual_pct": 12.0, "evidence": ["check:chk_2"]}])
    assert any("residual is 41.2" in p for p in citation_problems(wrong_residual, EVIDENCE, CHECKS))
    wrong_rule = diagnosis(physics=[{"rule_id": "inverter.load_pct_vs_va", "result": "fail", "residual_pct": None,
                                     "evidence": ["check:chk_2"]}])
    assert any("is rule inverter.dc_power_balance" in p for p in citation_problems(wrong_rule, EVIDENCE, CHECKS))


def test_observations_need_evidence_and_unsafe_actions_are_refused():
    no_evidence = diagnosis(observations=[{"statement": "Something is off", "evidence": []}])
    assert any("without evidence" in p for p in citation_problems(no_evidence, EVIDENCE, CHECKS))
    for action in ("Open the battery box and check the cells", "Bypass the low-voltage cut-off to test"):
        assert any("unsafe" in p for p in citation_problems(diagnosis(proposed_action=action), EVIDENCE, CHECKS))


def test_the_template_states_only_stored_facts():
    checks = [{"check_id": "chk_2", "rule_id": "inverter.dc_power_balance", "outcome": "persistent_violation",
               "expected": 402.0, "observed": 568.0, "residual_pct": 41.2, "reason": "residual +41.2 %"}]
    t = template_diagnosis({"anomaly_id": "an_1"}, checks, simulated=True)
    assert t.headline.startswith("SIMULATED: ") and t.confidence == "low"
    assert t.observations[0].evidence == ["check:chk_2"] and t.physics[0].residual_pct == 41.2
    card_line = "- check:chk_2: inverter.dc_power_balance persistent_violation; expected 402.0, observed 568.0"
    assert citation_problems(t, EVIDENCE + card_line, CHECKS) == []    # grounded in what the card shows


def test_no_dashes_keeps_meaning_without_em_or_en_dashes():
    from backend.app.agent.guardrails import no_dashes, no_dashes_in

    assert no_dashes("That is the max \u2014 the readings are 230 V.") == "That is the max, the readings are 230 V."
    assert no_dashes("Clamp 29.4 A \u2014 current, not voltage.") == "Clamp 29.4 A, current, not voltage."
    assert no_dashes("Load was 10\u201320 % all clip.") == "Load was 10-20 % all clip."
    assert no_dashes("\u2014 first point\n\u2013 second") == "- first point\n- second"
    assert no_dashes("It ended here \u2014.") == "It ended here."
    assert no_dashes("No dashes here.") == "No dashes here."
    assert no_dashes_in({"a": ["x \u2014 y"], "n": 3}) == {"a": ["x, y"], "n": 3}


def test_an_empty_diagnosis_is_never_verified():
    empty = diagnosis().model_copy(update={"observations": [], "physics": [], "hypotheses": []})
    problems = citation_problems(empty, EVIDENCE, CHECKS)
    assert {"the diagnosis has no observations", "the diagnosis has no physics findings",
            "the diagnosis has no hypotheses"} <= set(problems)


def test_invented_numbers_are_caught_outside_observations_too():
    made_up = diagnosis().model_copy(update={"headline": "The bank lost 87.5 percent of its capacity"})
    assert "number 87.5 not in any tool result" in citation_problems(made_up, EVIDENCE, CHECKS)
    counted = diagnosis().model_copy(update={"headline": "2 readings disagree"})     # small counts aren't readings
    assert citation_problems(counted, EVIDENCE, CHECKS) == []


def test_a_diagnosis_cant_round_a_reading_to_an_integer():
    assert _supported("25", {25.2}) and not _supported("25", {25.2}, exact_integers=True)
    assert _supported("25", {25.0}, exact_integers=True) and _supported("25.2", {25.2}, exact_integers=True)


def test_a_physics_finding_must_state_what_the_check_concluded():
    finding = diagnosis().physics[0]
    ref = finding.evidence[0].split(":", 1)[1]
    checks = {**CHECKS, ref: {**CHECKS[ref], "outcome": "persistent_violation"}}
    wrong = diagnosis().model_copy(update={"physics": [finding.model_copy(update={"result": "pass"})]})
    assert any("concluded persistent_violation" in p for p in citation_problems(wrong, EVIDENCE, checks))
    for said in ("fail", "Failed", "persistent_violation"):
        fine = diagnosis().model_copy(update={"physics": [finding.model_copy(update={"result": said})]})
        assert not any("concluded" in p for p in citation_problems(fine, EVIDENCE, checks))


def test_the_safety_screen_catches_live_parts_and_padding_but_not_warnings():
    assert unsafe_in("Touch the battery terminals to feel if they are warm")
    assert unsafe_in("Open up, carefully and with a good torch in hand, the front cover")
    assert unsafe_in("Bypass the low voltage protection for now")
    assert not unsafe_in("Do not touch the battery terminals.")
    assert not unsafe_in("Never open the enclosure; read the display from outside.")
    assert not unsafe_in("Compare the display with a clamp meter under the same load.")
    assert unsafe_in("Don't wait. Touch the terminals now.")                 # a warning covers its own sentence only
