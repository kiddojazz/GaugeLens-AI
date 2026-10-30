"""Runs the physics engine inside process_clip: one role state per equipment, fed frame by frame.

  - readings from linked equipment (a DC clamp meter on the inverter's battery cable) feed the same state
  - after each frame, rules touched by FRESHLY read roles are evaluated (reused crops only refresh ages)
  - every result is stored in physics_checks (skipped ones too, with the reason: rule coverage metric)
  - a reading in a passing check becomes 'accepted'
  - a failing check with a freshly read suspect triggers the RE-LOOK (relook.py, up to 2 attempts):
        re-read value makes the failing rules pass -> new reading 'corrected' (supersedes), old 'rejected',
                                                      outcome 'misread_fixed'
        re-read confirms the original value        -> outcome 'persistent_violation' (a real fault?)
        otherwise                                   -> suspect 'needs_review', outcome 'needs_review'
"""

import logging
from dataclasses import replace
from datetime import timedelta

from ..ids import new_id
from ..jobs.events import emit
from ..physics.engine import CheckResult, attribute, evaluate
from ..physics.rules import PackDefaults, Rule, RulePack, load_pack
from ..physics.state import RoleReading, RoleState

log = logging.getLogger(__name__)

_PACK_FOR_KIND = {"inverter": "inverter", "generator": "generator", "pressure_system": "pressure", "meter": "meter"}
PERSISTENT_MIN_CONFIDENCE = 0.8

_INSERT_CHECK = """
INSERT INTO physics_checks (check_id, ts, equipment_id, clip_id, frame_id, rule_id, reading_ids, expected, observed,
    residual_pct, tolerance_used, passed, steady_state, input_ages_s, suspect_reading_id, candidate_fix, outcome, reason,
    relook)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
"""
_INSERT_RELOOK_READING = """
INSERT INTO readings (ts, reading_id, instrument_id, frame_id, value, unit, raw_text, display_page, method, confidence,
                      status, attempt, supersedes, crop_uri, model_call_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'llm', $9, $10, $11, $12, $13, $14)
ON CONFLICT (ts, frame_id, instrument_id, attempt) DO NOTHING
"""


def pack_name_for(equipment: dict) -> str | None:
    if equipment["kind"] == "charge_controller":
        return "cc_pwm" if equipment.get("controller_type") == "pwm" else "cc_mppt"
    return _PACK_FOR_KIND.get(equipment["kind"])


def constants_for(equipment: dict) -> dict:
    def num(v):
        return float(v) if v is not None else None
    limits = {k: num(v) for k, v in ((equipment.get("profile") or {}).get("limits") or {}).items()}
    return {"nominal_v": num(equipment.get("nominal_battery_v")), "rated_va": num(equipment.get("rated_va")),
            "idle_w": num(equipment.get("idle_w")), **limits}       # nameplate limits of identified equipment


# Units of running totals: whatever the device, these never go down (a drop means a misread or a reset).
CUMULATIVE_UNITS = {"kwh", "wh", "mwh", "kvarh", "m3", "l", "gal", "h", "km", "mi"}


def _cumulative(instrument: dict) -> bool:
    unit = "".join((instrument.get("unit") or "").split()).lower().replace("³", "3")
    return bool((instrument.get("match") or {}).get("cumulative")) or unit in CUMULATIVE_UNITS


def pack_for(equipment: dict, instruments: list[dict]) -> RulePack | None:
    """The equipment kind's rule pack, plus generic rules for cumulative readings it doesn't cover (identified
    equipment of any kind: a counter never goes backwards). None when there is nothing to check."""
    name = pack_name_for(equipment)
    pack = load_pack(name) if name else None
    covered = {role for rule in pack.rules for role in rule.inputs} if pack else set()
    generic = [Rule(id=f"generic.{i['physics_role']}_monotonic", kind="transition", inputs=[i["physics_role"]],
                    description=f"{i.get('label') or i['physics_role']} is a running total: it never goes down",
                    check=f"delta({i['physics_role']}) >= 0", severity_on_persist="warning")
               for i in instruments if _cumulative(i) and i["physics_role"] not in covered]
    if pack is None:
        if not generic:
            return None
        return RulePack(equipment_kind=equipment["kind"], prefix="generic.", rules=generic,
                        defaults=PackDefaults(max_age_s=60, requires_steady=False))
    return pack.model_copy(update={"rules": [*pack.rules, *generic]}) if generic else pack


class PhysicsStage:
    def __init__(self, pool, clip: dict, equipment: dict, pack: RulePack, max_relooks: int = 10):
        self.pool, self.clip, self.equipment, self.pack = pool, clip, equipment, pack
        self.constants = constants_for(equipment)
        self.state = RoleState(steady_role=pack.steady.role, step_pct=pack.steady.step_pct)
        self.max_relooks = max_relooks
        self.totals = {"checks_pass": 0, "checks_fail": 0, "checks_skipped": 0, "relooks": 0, "misread_fixed": 0,
                       "persistent_violation": 0, "anomalies": 0}
        self.anomalies: dict[str, str] = {}          # rule_id -> open anomaly_id in this clip
        self.recent: dict[str, dict] = {}            # reading_id -> fresh row (incl. frame_pos), for re-looks
        self.relooked: dict[str, str] = {}           # reading_id -> re-look outcome (never re-look twice)

    @classmethod
    async def load(cls, pool, clip: dict, max_relooks: int = 10, timed: bool = True) -> "PhysicsStage | None":
        """timed=False: photos without capture times. Rules that divide by the time between readings (rates)
        can't run; the rest do, with values from one photo never combined with another's (photos are far apart)."""
        equipment = await pool.fetchrow("SELECT * FROM equipment WHERE equipment_id = $1", clip["equipment_id"])
        if equipment is None:
            return None
        instruments = [dict(r) for r in await pool.fetch(
            """SELECT physics_role, label, unit, match FROM instruments
               WHERE equipment_id = $1 OR equipment_id IN (SELECT equipment_id FROM equipment WHERE linked_equipment_id = $1)""",
            equipment["equipment_id"])]
        pack = pack_for(dict(equipment), instruments)
        if pack is not None and not timed:
            pack = pack.model_copy(update={"rules": [r for r in pack.rules
                                                     if "delta_t" not in f"{r.check} {r.check_ratio}"]})
            pack = pack if pack.rules else None
        if pack is None:
            log.info("no physics rules apply to equipment %s (%s)", equipment["equipment_id"], equipment["kind"])
            return None
        return cls(pool, dict(clip), dict(equipment), pack, max_relooks)

    def _feed(self, row: dict, t: float) -> None:
        if row.get("physics_role") and row.get("value") is not None:
            self.state.update(RoleReading(
                role=row["physics_role"], value=float(row["value"]), ts=t, reading_id=row["reading_id"],
                confidence=float(row.get("confidence") or 0.0), raw_text=row.get("raw_text"),
                resolution=float(row["resolution"]) if row.get("resolution") is not None else None))

    async def observe(self, frame_id: str, t: float, rows: list[dict], fresh_roles: set[str],
                      relooker=None) -> tuple[list[dict], list[dict]]:
        """rows: readings stored for this frame (fresh or reused). relooker(suspect_row, attempt) -> RelookResult.
        Returns (compact results for the frame event, corrections [{display_id, instrument_id, row}])."""
        for row in rows:
            self._feed(row, t)
        if not fresh_roles:
            return [], []
        results = evaluate(self.pack, self.state, self.constants, now=t, roles=fresh_roles)
        attribution = None
        if any(r.status == "fail" for r in results):
            attribution = attribute(self.pack, self.state, self.constants, results, now=t)

        relook, corrections, outcome_for_fail = None, [], "needs_review"
        for row in rows:                                 # reused copies carry origin_id + the origin's frame_pos
            self.recent[row["reading_id"]] = row
        suspect_id = attribution.suspect_reading_id if attribution else None
        suspect_row = self.recent.get(suspect_id) if suspect_id else None
        origin = (suspect_row.get("origin_id") or suspect_id) if suspect_row else suspect_id
        if origin in self.relooked:                      # already re-looked: reuse its verdict, no new model calls
            outcome_for_fail = self.relooked[origin]
            if outcome_for_fail == "persistent_violation":
                self.totals["persistent_violation"] += 1
        elif suspect_row and relooker and self.totals["relooks"] < self.max_relooks:
            self.totals["relooks"] += 1
            relook, outcome_for_fail, correction = await self._relook(frame_id, t, suspect_row, results, relooker)
            if outcome_for_fail == "persistent_violation":   # a jump compares two readings: the earlier may be wrong
                earlier = await self._relook_previous(frame_id, t, suspect_row, results, relooker)
                if earlier:
                    relook, outcome_for_fail = earlier
                    correction = None
                    if outcome_for_fail != "persistent_violation":     # the first verdict no longer stands
                        self.totals["persistent_violation"] -= 1
            self.relooked[origin] = outcome_for_fail
            if correction:
                corrections.append(correction)

        compact = await self._store(frame_id, t, results, attribution, relook, outcome_for_fail)
        # failed rules, rechecked on the fix; a re-look that only confirmed the value changed nothing to recheck
        if any("row" in c for c in corrections) or (relook and outcome_for_fail == "misread_fixed"):
            failed = {r.rule_id for r in results if r.status == "fail"}
            rechecked = [r for r in evaluate(self.pack, self.state, self.constants, now=t, roles=fresh_roles)
                         if r.rule_id in failed]
            compact += await self._store(frame_id, t, rechecked, None, None, "needs_review")
        return compact, corrections

    async def _relook_previous(self, frame_id: str, t: float, current: dict, results: list[CheckResult], relooker):
        """The current reading was confirmed but a transition rule (rate, monotonic) still fails: re-look the
        reading it was compared with, on its own frame. A misread first frame must not open a fault."""
        rules = {rule.id: rule for rule in self.pack.rules}
        if not any(rules[r.rule_id].kind == "transition" for r in results if r.status == "fail"):
            return None
        role = current["physics_role"]
        before = self.state.previous.get(role)
        row = self.recent.get(before.reading_id) if before and before.reading_id else None
        if row is None:
            return None
        origin = row.get("origin_id") or row["reading_id"]
        if origin in self.relooked or self.totals["relooks"] >= self.max_relooks:
            return None
        self.totals["relooks"] += 1
        relook, outcome, _ = await self._relook(frame_id, t, row, results, relooker, previous=True)
        self.relooked[origin] = outcome
        return relook, outcome

    async def _relook(self, frame_id: str, t: float, suspect: dict, results: list[CheckResult], relooker,
                      previous: bool = False):
        failing = {r.rule_id for r in results if r.status == "fail"}
        role, original = suspect["physics_role"], float(suspect["value"])
        attempts, outcome, correction = [], "needs_review", None
        for attempt in (2, 3):
            res = await relooker(suspect, attempt)
            if res is None:
                break
            entry = {"attempt": attempt, "strategy": res.strategy, "value": res.value, "raw_text": res.raw_text,
                     "unit": res.unit, "legible": res.legible, "crop_uri": res.crop_uri, "call_ids": res.call_ids,
                     "detail": res.detail, "reading_id": new_id("rd_"),
                     "decoder": res.detail.get("decoder") if res.detail else None}
            attempts.append(entry)
            # Candidates from independent readers (Muse Spark, the seven-segment decoder); physics arbitrates.
            candidates = [(res.value, res.raw_text, res.strategy)] if (res.value is not None and res.legible) else []
            candidates += [c for c in res.alternatives if all(abs(c[0] - k[0]) > 1e-9 for k in candidates)]
            if not candidates:
                entry["outcome"] = "illegible"
                continue
            if all(abs(c[0] - original) < 1e-9 for c in candidates):
                entry["outcome"] = "confirmed"
                confident = all(self.state.current[r].confidence >= PERSISTENT_MIN_CONFIDENCE
                                for rule in self.pack.rules if rule.id in failing for r in rule.inputs
                                if r in self.state.current)
                outcome = "persistent_violation" if confident else "needs_review"
                break
            passing = []
            for value, raw, strategy in candidates:
                if abs(value - original) < 1e-9:
                    continue
                trial_state = (self.state.with_previous_value(role, value) if previous
                               else self.state.with_value(role, value))
                trial = evaluate(self.pack, trial_state, self.constants, now=t, roles={role})
                # the rules this value feeds: the failed ones must now pass, and none may start failing. Failures in
                # rules that don't use it can't be fixed by re-reading it, so they don't count against the fix.
                repaired = [tr for tr in trial if tr.rule_id in failing]
                if repaired and all(tr.status == "pass" for tr in repaired) and not any(tr.status == "fail" for tr in trial):
                    passing.append((value, raw, strategy))
            if len(passing) == 1:
                entry["value"], entry["raw_text"], entry["strategy"] = passing[0]
                entry["outcome"] = "fixed"
                outcome = "misread_fixed"
                break
            entry["outcome"] = "ambiguous" if passing else "still_failing"
            if passing:
                break

        for entry in attempts:
            fixed = entry.get("outcome") == "fixed"
            read_frame = (entry.get("detail") or {}).get("frame_id") or frame_id   # where the re-read happened
            read_t = (entry.get("detail") or {}).get("t_offset_s", t)
            ts = self.clip["recorded_at"] + timedelta(seconds=read_t)
            entry["frame_id"] = read_frame
            await self.pool.execute(
                _INSERT_RELOOK_READING, ts, entry["reading_id"], suspect["instrument_id"], read_frame, entry["value"],
                entry["unit"] or suspect.get("unit"), entry["raw_text"], suspect.get("display_page"),
                0.9 if fixed else (0.0 if entry["value"] is None else 0.5),
                "corrected" if fixed else "needs_review", entry["attempt"], suspect["reading_id"], entry["crop_uri"],
                entry["call_ids"][-1] if entry["call_ids"] else None)
            await emit(self.pool, self.clip["clip_id"], "relook_attempt", {
                "frame_id": frame_id, "t_offset_s": t, "suspect_reading_id": suspect["reading_id"], "role": role,
                "original_value": original, "original_raw_text": suspect.get("raw_text"),
                "original_crop_uri": suspect.get("crop_uri"), **{k: v for k, v in entry.items() if k != "call_ids"}})

        new_status = {"misread_fixed": "rejected", "persistent_violation": "accepted"}.get(outcome, "needs_review")
        await self.pool.execute("UPDATE readings SET status = $2 WHERE reading_id = $1", suspect["reading_id"], new_status)
        if outcome == "misread_fixed":
            fixed = next(e for e in attempts if e.get("outcome") == "fixed")
            self.totals["misread_fixed"] += 1
            row = {**suspect, "reading_id": fixed["reading_id"], "value": fixed["value"], "raw_text": fixed["raw_text"],
                   "status": "corrected", "attempt": fixed["attempt"], "crop_uri": fixed["crop_uri"], "confidence": 0.9}
            if previous:                                  # the earlier reading was wrong: correct history, not "now"
                self.state.previous[role] = replace(self.state.previous[role], value=float(fixed["value"]),
                                                    reading_id=fixed["reading_id"], raw_text=fixed["raw_text"])
            else:
                self._feed(row, t)
            correction = {"display_id": suspect.get("display_id"), "instrument_id": suspect["instrument_id"], "row": row}
        else:
            if outcome == "persistent_violation":
                self.totals["persistent_violation"] += 1
            # later frames that reuse this crop must carry the same status (not 'pending')
            correction = {"display_id": suspect.get("display_id"), "instrument_id": suspect["instrument_id"],
                          "status": new_status}
        relook = {"suspect_reading_id": suspect["reading_id"], "outcome": outcome,
                  "attempts": [{k: v for k, v in e.items() if k not in ("detail",)} for e in attempts]}
        return relook, outcome, correction

    async def _record_anomaly(self, r: CheckResult, check_id: str, ts, frame_id: str, reading_ids: list) -> None:
        """A confirmed violation (the re-look re-read the same value) is a real fault candidate: open an
        anomaly, or extend the one already open for this rule in this clip."""
        rule = next(x for x in self.pack.rules if x.id == r.rule_id)
        anomaly_id = self.anomalies.get(r.rule_id)
        if anomaly_id is None:
            anomaly_id = new_id("an_")
            self.anomalies[r.rule_id] = anomaly_id
            self.totals["anomalies"] += 1
            summary = f"{rule.description or rule.id}: confirmed by re-look ({r.reason})"
            await self.pool.execute(
                """INSERT INTO anomalies (anomaly_id, ts_start, ts_end, equipment_id, kind, severity, summary, evidence,
                                          clip_id, rule_id, is_simulated)
                   VALUES ($1, $2, $2, $3, 'physics', $4, $5, $6, $7, $8, $9)""",
                anomaly_id, ts, self.equipment["equipment_id"], rule.severity_on_persist, summary,
                {"check_ids": [check_id], "reading_ids": reading_ids, "frame_ids": [frame_id]},
                self.clip["clip_id"], r.rule_id, bool(self.clip.get("is_demo_replay")))
            await emit(self.pool, self.clip["clip_id"], "anomaly", {
                "anomaly_id": anomaly_id, "rule_id": r.rule_id, "severity": rule.severity_on_persist,
                "summary": summary, "frame_id": frame_id, "simulated": bool(self.clip.get("is_demo_replay"))})
        else:
            await self.pool.execute(
                """UPDATE anomalies SET ts_end = $2, evidence = jsonb_set(jsonb_set(jsonb_set(evidence,
                       '{check_ids}', (evidence->'check_ids') || to_jsonb($3::text)),
                       '{frame_ids}', (evidence->'frame_ids') || to_jsonb($4::text)),
                       '{reading_ids}', (evidence->'reading_ids') || to_jsonb($5::text[]))
                   WHERE anomaly_id = $1""", anomaly_id, ts, check_id, frame_id, reading_ids)

    async def _store(self, frame_id: str, t: float, results: list[CheckResult], attribution, relook,
                     outcome_for_fail: str) -> list[dict]:
        ts = self.clip["recorded_at"] + timedelta(seconds=t)
        compact = []
        for r in results:
            self.totals[f"checks_{r.status}"] += 1
            failed = r.status == "fail"
            suspect = attribution.suspect_reading_id if (attribution and failed) else None
            fix = attribution.candidate_fix if (attribution and failed) else None
            outcome = {"pass": "pass", "skipped": "skipped"}.get(r.status, outcome_for_fail)
            check_id = new_id("chk_")
            ids = [i for i in r.reading_ids if i]
            await self.pool.execute(
                _INSERT_CHECK, check_id, ts, self.equipment["equipment_id"], self.clip["clip_id"], frame_id, r.rule_id,
                ids, r.expected, r.observed, r.residual_pct, r.tolerance_used, r.status == "pass", r.steady,
                r.input_ages or None, suspect, fix, outcome, r.reason, relook if failed else None)
            if r.status == "skipped":
                continue
            if failed and outcome == "persistent_violation":
                await self._record_anomaly(r, check_id, ts, frame_id, ids)
            if r.status == "pass":
                await self.pool.execute(
                    "UPDATE readings SET status = 'accepted' WHERE reading_id = ANY($1::text[]) AND status = 'pending'", ids)
            elif suspect and outcome == "needs_review":
                await self.pool.execute(
                    "UPDATE readings SET status = 'needs_review' WHERE reading_id = $1 AND status IN ('pending', 'accepted')",
                    suspect)
            await emit(self.pool, self.clip["clip_id"], "physics_check", {
                "check_id": check_id, "frame_id": frame_id, "t_offset_s": t, "rule_id": r.rule_id, "status": r.status,
                "outcome": outcome, "residual_pct": r.residual_pct, "tolerance_pct": r.tolerance_used,
                "reading_ids": r.reading_ids, "suspect_reading_id": suspect, "candidate_fix": fix,
                "reason": attribution.reason if suspect else r.reason})
            compact.append({"rule_id": r.rule_id, "status": r.status, "outcome": outcome, "suspect": suspect})
        return compact
