"""Deterministic checks on a diagnosis before anyone sees it (GridIQ's grounding check, extended):
  - every observation cites at least one ID, and every cited ID was returned by a tool or shown in the card
  - every number in an observation appears in the evidence text (tool results + card), at its precision
  - every physics finding matches the stored check exactly (rule id, outcome family, residual to 0.1 %)
  - the proposed action is not an unsafe instruction
Problems -> one retry with feedback, then the deterministic template (template_diagnosis)."""

import re

from .schemas import DiagnosisOutput, Hypothesis, Observation, PhysicsFinding

ID = re.compile(r"\b(?:reading|check|frame|anomaly|manual):[A-Za-z0-9_#.\-]+")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
_UNSAFE = [
    re.compile(r"\b(open|remove|unscrew)\b[^.]{0,40}\b(cover|enclosure|panel|battery box|casing)\b", re.IGNORECASE),
    re.compile(r"\b(bypass|disable|override|jumper)\b[^.]{0,40}\b(protection|breaker|fuse|cut-?off|bms)\b", re.IGNORECASE),
]


def ids_in(text: str) -> set[str]:
    return set(ID.findall(text or ""))


def numbers_in(text: str) -> set[float]:
    return {float(m) for m in _NUMBER.findall(ID.sub(" ", text or ""))}


def _supported(token: str, evidence: set[float]) -> bool:
    value = float(token)
    places = len(token.split(".")[1]) if "." in token else 0
    return any(round(v, places) == round(value, places) or abs(v - value) < 1e-9 for v in evidence)


def citation_problems(diagnosis: DiagnosisOutput, evidence_text: str, checks: dict[str, dict]) -> list[str]:
    """`evidence_text`: the card plus every tool result of this run. `checks`: stored physics_checks by id."""
    known_ids = ids_in(evidence_text)
    known_numbers = numbers_in(evidence_text)
    problems: list[str] = []
    for obs in diagnosis.observations:
        if not obs.evidence:
            problems.append(f"observation without evidence: '{obs.statement[:60]}'")
        for ref in obs.evidence:
            if ref not in known_ids:
                problems.append(f"unknown evidence id {ref}")
        for token in _NUMBER.findall(ID.sub(" ", obs.statement)):
            if not _supported(token, known_numbers):
                problems.append(f"number {token} not in any tool result")
    for finding in diagnosis.physics:
        refs = [r for r in finding.evidence if r.startswith("check:")]
        if not refs:
            problems.append(f"physics finding {finding.rule_id} cites no check")
        for ref in refs:
            stored = checks.get(ref.split(":", 1)[1])
            if ref not in known_ids or stored is None:
                problems.append(f"unknown check {ref}")
                continue
            if stored["rule_id"] != finding.rule_id:
                problems.append(f"{ref} is rule {stored['rule_id']}, not {finding.rule_id}")
            if (finding.residual_pct is not None and stored["residual_pct"] is not None
                    and round(float(stored["residual_pct"]), 1) != round(finding.residual_pct, 1)):
                problems.append(f"{ref} residual is {stored['residual_pct']}, not {finding.residual_pct}")
    for hyp in diagnosis.hypotheses:
        for ref in [r for r in hyp.supporting + hyp.against if ID.fullmatch(r)]:
            if ref not in known_ids:
                problems.append(f"unknown evidence id {ref} in hypothesis '{hyp.cause[:40]}'")
    for pattern in _UNSAFE:
        if pattern.search(diagnosis.proposed_action):
            problems.append("the proposed action is unsafe (opening enclosures or defeating protection)")
    return list(dict.fromkeys(problems))


def template_diagnosis(anomaly: dict, checks: list[dict], simulated: bool) -> DiagnosisOutput:
    """Honest fallback when the agent's diagnosis can't be verified: only stored facts, no interpretation."""
    first = checks[0] if checks else None
    prefix = "SIMULATED: " if simulated else ""
    observations = []
    physics = []
    if first:
        observations.append(Observation(
            statement=(f"Rule {first['rule_id']} failed: expected {first['expected']}, observed {first['observed']}"
                       if first["expected"] is not None else f"Rule {first['rule_id']} failed: {first['reason']}"),
            evidence=[f"check:{first['check_id']}"]))
        physics = [PhysicsFinding(rule_id=c["rule_id"], result=c["outcome"], residual_pct=c["residual_pct"],
                                  evidence=[f"check:{c['check_id']}"]) for c in checks[:3]]
    return DiagnosisOutput(
        headline=f"{prefix}A physics check failed and the re-look confirmed the readings; needs expert review",
        observations=observations, physics=physics,
        hypotheses=[Hypothesis(cause="Not determined automatically", likelihood="low", supporting=[], against=[])],
        unknowns=["The automated diagnosis could not be verified against the evidence"],
        proposed_action="Have a qualified technician compare the displayed values with an independent meter "
                        "under the same load.",
        safety_note="Do not open enclosures or touch battery terminals without isolating the equipment first.",
        confidence="low")


_RANGE_DASH = re.compile(r"(\d)\s*[\u2013\u2014]\s*(\d)")
_LEAD_DASH = re.compile(r"(?m)^(\s*)[\u2013\u2014]\s*")
_CLAUSE_DASH = re.compile(r"\s*[\u2013\u2014]\s*")
_STRAY_COMMA = re.compile(r",\s*(?=[,.;:!?)]|$)", re.MULTILINE)


def no_dashes(text: str) -> str:
    """House style: no em or en dashes. Ranges become "10-20", a dash between clauses becomes a comma."""
    text = _RANGE_DASH.sub(r"\1-\2", text)
    text = _LEAD_DASH.sub(r"\1- ", text)
    text = _CLAUSE_DASH.sub(", ", text)
    return _STRAY_COMMA.sub("", text)


def no_dashes_in(value):
    """no_dashes over every string in a JSON-like value (a diagnosis)."""
    if isinstance(value, str):
        return no_dashes(value)
    if isinstance(value, list):
        return [no_dashes_in(v) for v in value]
    if isinstance(value, dict):
        return {k: no_dashes_in(v) for k, v in value.items()}
    return value
