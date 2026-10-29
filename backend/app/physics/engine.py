"""Evaluates rule packs against the role state, and attributes failures to a suspect reading.

evaluate(): per rule -> pass | fail | skipped (missing or stale inputs, not steady, precondition false).
Tolerance = max(rule tolerance [low-load variant when it applies], display-rounding tolerance), where the
rounding tolerance is the sum of each input's half display step relative to its value (first-order
error propagation for products and quotients; conservative for sums).

attribute(): which reading broke physics? For every input of the failing rules, try the misreads a
display really produces (x10, /10, x2, /2, seven-segment confusions such as 3<->8 and 7<->9). A variant
is a valid fix if it makes every failing rule pass without breaking a passing one.
  - exactly one fix             -> candidate_fix (it only chooses what to re-look at; never stored as a reading)
  - several fixes, one reading  -> suspect that reading, value ambiguous
  - fixes on several readings   -> ambiguous; rank by confidence, jump since last value, age
  - no fix                      -> rank all inputs the same way
"""

import itertools
import re
from dataclasses import dataclass, field

from .rules import Rule, RulePack
from .safe_eval import MissingInput, safe_eval
from .state import RoleReading, RoleState

# What a seven-segment digit is most often misread as (Spike B: 7 -> 9 on the clamp meter).
SEGMENT_CONFUSIONS = {"0": "8", "1": "7", "2": "7", "3": "89", "5": "69", "6": "58", "7": "19", "8": "0369",
                      "9": "378"}


@dataclass
class CheckResult:
    rule_id: str
    status: str                                  # pass | fail | skipped
    reason: str = ""
    expected: float | None = None
    observed: float | None = None
    residual_pct: float | None = None
    tolerance_used: float | None = None
    reading_ids: list = field(default_factory=list)
    input_ages: dict = field(default_factory=dict)
    steady: bool | None = None
    low_load: bool = False
    severity: str = "warning"


@dataclass
class Attribution:
    suspect_reading_id: str | None
    suspect_role: str | None
    candidate_fix: dict | None
    reason: str
    ranking: list = field(default_factory=list)


def _rounding_tolerance(readings: list[RoleReading]) -> float:
    total = 0.0
    for r in readings:
        if r.resolution and r.value:
            total += (r.resolution / 2) / abs(r.value) * 100
    return total


def check_rule(pack: RulePack, rule: Rule, state: RoleState, constants: dict, now: float) -> CheckResult:
    result = CheckResult(rule_id=rule.id, status="skipped", severity=rule.severity_on_persist)
    readings = {role: state.current.get(role) for role in rule.inputs}
    missing = [role for role, r in readings.items() if r is None or r.value is None]
    if missing:
        result.reason = f"missing inputs: {', '.join(missing)}"
        return result
    result.reading_ids = [r.reading_id for r in readings.values()]
    max_age = rule.max_age_s if rule.max_age_s is not None else pack.defaults.max_age_s
    result.input_ages = {role: round(now - r.ts, 3) for role, r in readings.items()}
    stale = [f"{role} ({age:.0f} s)" for role, age in result.input_ages.items() if age > max_age]
    if stale:
        result.reason = f"stale inputs (> {max_age:.0f} s): {', '.join(stale)}"
        return result
    times = [r.ts for r in readings.values()]
    same_instant = max(times) - min(times) < 1e-6
    # Values read from the same frame are physically consistent whatever the load is doing; the steady
    # requirement only guards rules that combine values read at different moments (cycling pages).
    result.steady = True if same_instant else state.steady_between(min(times), max(times), pack.steady.settle_s)
    requires_steady = pack.defaults.requires_steady if rule.requires_steady is None else rule.requires_steady
    if requires_steady and not result.steady:
        result.reason = "not steady: a load step happened while these inputs were read"
        return result

    names = {**pack.constants, **{k: v for k, v in constants.items() if v is not None},
             **{k: v for k, v in rule.params.items() if not isinstance(v, list)},
             **{role: r.value for role, r in readings.items()}}
    deltas = None
    if rule.kind == "transition":
        previous = {role: state.previous.get(role) for role in rule.inputs}
        if any(p is None or p.value is None for p in previous.values()):
            result.reason = "transition rule needs a previous reading of every input"
            return result
        deltas = {role: readings[role].value - previous[role].value for role in rule.inputs}
        names["delta_t"] = max(readings[role].ts - previous[role].ts for role in rule.inputs)

    bands = {k: v for k, v in rule.params.items() if isinstance(v, list)}
    combos = [dict(zip(bands, values)) for values in itertools.product(*bands.values())] or [{}]
    rounding = _rounding_tolerance(list(readings.values()))
    try:
        if rule.when and not safe_eval(rule.when, names, deltas):
            result.reason = f"precondition false: {rule.when}"
            return result
        if rule.check is not None:
            ok = any(safe_eval(rule.check, {**names, **c}, deltas) for c in combos)
            result.status, result.reason = ("pass" if ok else "fail"), rule.check
            return result
        if rule.check_ratio is not None:
            observed = safe_eval(rule.check_ratio["observed"], names, deltas)
            expected = safe_eval(rule.check_ratio["expected"], names, deltas)
            if expected == 0:
                raise MissingInput("<expected is zero>")
            lo, hi = rule.check_ratio["band"]
            lo, hi = lo * (1 - rounding / 100), hi * (1 + rounding / 100)
            ratio = observed / expected
            bound = lo if ratio < lo else hi if ratio > hi else None
            result.expected, result.observed, result.tolerance_used = expected, observed, round(rounding, 3)
            result.residual_pct = 0.0 if bound is None else round((ratio - bound) / bound * 100, 3)
            result.status = "pass" if bound is None else "fail"
            result.reason = f"ratio {ratio:.3f} vs band [{lo:.3f}, {hi:.3f}]"
            return result

        observed = safe_eval(rule.observed, names, deltas)
        candidates = []
        for c in combos:
            expected = safe_eval(rule.expected, {**names, **c}, deltas)
            if expected == 0:
                raise MissingInput("<expected is zero>")
            candidates.append((expected, (observed - expected) / expected * 100))
        residuals = [res for _, res in candidates]
        if min(residuals) <= 0 <= max(residuals):
            residual = 0.0                                       # the observed value lies inside the band
            expected = observed
        else:
            expected, residual = min(candidates, key=lambda c: abs(c[1]))
        tolerance = rule.tolerance_pct or 0.0
        if rule.tolerance_pct_low_load is not None and rule.low_load_when:
            try:
                result.low_load = bool(safe_eval(rule.low_load_when, names, deltas))
            except MissingInput:
                result.low_load = False
            if result.low_load:
                tolerance = rule.tolerance_pct_low_load
        result.tolerance_used = round(max(tolerance, rounding), 3)
        result.expected, result.observed, result.residual_pct = expected, observed, round(residual, 3)
        result.status = "pass" if abs(residual) <= result.tolerance_used else "fail"
        result.reason = f"residual {residual:+.1f} % vs tolerance {result.tolerance_used:.1f} %"
        return result
    except MissingInput as exc:
        result.status, result.reason = "skipped", f"missing value: {exc.name}"
        return result


def evaluate(pack: RulePack, state: RoleState, constants: dict, now: float,
             roles: set[str] | None = None) -> list[CheckResult]:
    """All rules of the pack (or only those that use one of `roles`, e.g. the role just updated)."""
    return [check_rule(pack, rule, state, constants, now) for rule in pack.rules
            if roles is None or set(rule.inputs) & set(roles)]


# ------------------------------------------------------------------------------------- attribution ----

def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def _number_text(raw_text: str | None, value: float) -> str:
    """The displayed number with '.' as the decimal point. Displays differ ('2,388' is 2.388 m3 on a German water
    meter, '1,234.5' elsewhere): the reader's value says which separator was the decimal one."""
    raw = re.sub(r"[^0-9.,\-]", "", raw_text or "")
    candidates = [raw.replace(",", ""),                              # 1,234.5 -> 1234.5
                  raw.replace(".", "").replace(",", ".")]           # 1.234,5 -> 1234.5 and 2,388 -> 2.388
    for text in candidates:
        try:
            if abs(float(text) - value) <= 1e-9 * max(1.0, abs(value)):
                return text
        except ValueError:
            continue
    return f"{value:g}"


def misread_variants(reading: RoleReading) -> list[tuple[float, str]]:
    """Values the display could really have shown, given this (possibly wrong) reading."""
    value = reading.value
    variants: list[tuple[float, str]] = [(value / 10, "div10"), (value * 10, "x10"), (value / 2, "div2"),
                                         (value * 2, "x2")]
    raw = _number_text(reading.raw_text, value)
    for i, ch in enumerate(raw):
        for alt in SEGMENT_CONFUSIONS.get(ch, ""):
            candidate = raw[:i] + alt + raw[i + 1:]
            try:
                variants.append((float(candidate), f"seg_{ch}to{alt}"))
            except ValueError:
                continue
    places = max(_decimals(raw) + 1, 6)
    seen, unique = {round(value, places)}, []
    for v, kind in variants:
        key = round(v, places)
        if key not in seen:
            seen.add(key)
            unique.append((key, kind))
    return unique


def attribute(pack: RulePack, state: RoleState, constants: dict, results: list[CheckResult],
              now: float) -> Attribution:
    failing = [r.rule_id for r in results if r.status == "fail"]
    if not failing:
        return Attribution(None, None, None, "no failing rules")
    passing = {r.rule_id for r in results if r.status == "pass"}
    rules = {rule.id: rule for rule in pack.rules}
    roles = list(dict.fromkeys(role for rule_id in failing for role in rules[rule_id].inputs
                               if role in state.current and state.current[role].value is not None))

    fixes = []
    for role in roles:
        for value, kind in misread_variants(state.current[role]):
            trial = {r.rule_id: r.status for r in evaluate(pack, state.with_value(role, value), constants, now)}
            if all(trial.get(f) == "pass" for f in failing) and not any(trial.get(p) == "fail" for p in passing):
                fixes.append((role, value, kind))

    def rank(candidate_roles: list[str]) -> list[str]:
        def key(role):
            r = state.current[role]
            prev = state.previous.get(role)
            jump = abs(r.value - prev.value) / max(abs(prev.value), 1e-9) if prev and prev.value is not None else 0.0
            return (r.confidence, -jump, r.ts)
        return sorted(candidate_roles, key=key)

    fix_roles = list(dict.fromkeys(role for role, _, _ in fixes))
    if len(fixes) == 1:
        role, value, kind = fixes[0]
        reading = state.current[role]
        return Attribution(reading.reading_id, role,
                           {"role": role, "reading_id": reading.reading_id, "value": value, "kind": kind},
                           f"single explanation: {role} misread ({kind})", ranking=[reading.reading_id])
    if len(fix_roles) == 1:
        role = fix_roles[0]
        values = ", ".join(f"{v:g} ({k})" for _, v, k in fixes)
        return Attribution(state.current[role].reading_id, role, None,
                           f"ambiguous value: only {role} explains the failure, but {len(fixes)} values fit: {values}",
                           ranking=[state.current[role].reading_id])
    ranked = rank(fix_roles or roles)
    reason = (f"ambiguous: {len(fix_roles)} readings could explain the failure ({', '.join(fix_roles)})"
              if fix_roles else "no single digit or scale misread explains the failure")
    return Attribution(state.current[ranked[0]].reading_id, ranked[0], None, reason,
                       ranking=[state.current[r].reading_id for r in ranked])
