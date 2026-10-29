"""The context card given to the agent (GridIQ's schema card, for equipment): what the equipment is, which
rules apply, the anomaly (or the clip) with its evidence IDs, and live data coverage, including what was NEVER
on screen, so the agent knows what it can't know.

build_card(pool, anomaly)    the diagnosis agent: one anomaly
build_clip_card(pool, clip)  the chat: a whole clip (corrections, anomalies, check outcomes)
"""

from ..physics.rules import load_pack
from ..pipeline.physics_stage import pack_name_for


def _num(v):
    return f"{float(v):g}" if v is not None else "unknown"


async def _equipment_lines(pool, equipment_id: str) -> tuple[list[str], list[dict]]:
    equipment = dict(await pool.fetchrow("SELECT * FROM equipment WHERE equipment_id = $1", equipment_id))
    linked = [dict(r) for r in await pool.fetch("SELECT * FROM equipment WHERE linked_equipment_id = $1", equipment_id)]
    instruments = [dict(r) for r in await pool.fetch(
        """SELECT i.instrument_id, i.physics_role, i.label, i.unit, i.equipment_id FROM instruments i
           WHERE i.equipment_id = $1 OR i.equipment_id IN (SELECT equipment_id FROM equipment WHERE linked_equipment_id = $1)
           ORDER BY i.equipment_id, i.physics_role""", equipment_id)]
    lines = ["## Equipment",
             f"- {equipment['equipment_id']}: {equipment['kind']}, model \"{equipment.get('model') or 'unknown'}\", "
             f"nominal battery {_num(equipment.get('nominal_battery_v'))} V, rating {_num(equipment.get('rated_va'))} VA, "
             f"idle draw {_num(equipment.get('idle_w'))} W"]
    lines += [f"- linked: {e['equipment_id']} ({e['kind']}, \"{e.get('model') or ''}\")" for e in linked]
    lines.append("\n## Instruments (role: label, unit)")
    lines += [f"- {i['physics_role']}: {i['label']}, {i['unit']} ({i['equipment_id']})" for i in instruments]
    pack_name = pack_name_for(equipment)
    if pack_name:
        lines.append("\n## Physics rules for this equipment")
        for rule in load_pack(pack_name).rules:
            form = (f"expected {rule.expected}, observed {rule.observed}, tolerance {rule.tolerance_pct} %"
                    if rule.expected else rule.check or f"ratio {rule.check_ratio}")
            lines.append(f"- {rule.id}: {rule.description or ''} [{form}]")
    return lines, instruments


async def _coverage_lines(pool, clip_id: str, instruments: list[dict]) -> list[str]:
    coverage = {r["physics_role"]: dict(r) for r in await pool.fetch(
        """SELECT i.physics_role, count(*) AS n, min(f.t_offset_s) AS t0, max(f.t_offset_s) AS t1
           FROM readings r JOIN frames f USING (frame_id) JOIN instruments i USING (instrument_id)
           WHERE f.clip_id = $1 AND r.status <> 'rejected' GROUP BY i.physics_role""", clip_id)}
    span = await pool.fetchrow("SELECT count(*) AS n, max(t_offset_s) AS t1 FROM frames WHERE clip_id = $1", clip_id)
    lines = [f"\n## Data coverage (clip {clip_id}: {span['n']} frames, 0 to {span['t1']} s)"]
    for i in instruments:
        cov = coverage.get(i["physics_role"])
        if cov:
            lines.append(f"- {i['physics_role']}: {cov['n']} readings, t {cov['t0']} to {cov['t1']} s")
    never = [i["physics_role"] for i in instruments if i["physics_role"] not in coverage]
    lines.append(f"- NEVER on screen in this clip: {', '.join(never) if never else 'none'}")
    return lines


async def build_card(pool, anomaly: dict) -> str:
    clip_id = anomaly["clip_id"]
    clip = dict(await pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", clip_id))
    lines, instruments = await _equipment_lines(pool, anomaly["equipment_id"])
    t_start = await pool.fetchval("SELECT min(f.t_offset_s) FROM frames f WHERE f.frame_id = ANY($1::text[])",
                                  anomaly["evidence"].get("frame_ids", []))
    t_end = await pool.fetchval("SELECT max(f.t_offset_s) FROM frames f WHERE f.frame_id = ANY($1::text[])",
                                anomaly["evidence"].get("frame_ids", []))
    checks = [dict(r) for r in await pool.fetch(
        """SELECT check_id, rule_id, outcome, expected, observed, residual_pct, tolerance_used, reason
           FROM physics_checks WHERE check_id = ANY($1::text[]) ORDER BY ts LIMIT 5""",
        anomaly["evidence"].get("check_ids", []))]

    simulated = bool(anomaly.get("is_simulated") or clip.get("is_demo_replay"))
    lines += ["\n## The anomaly",
              f"- anomaly:{anomaly['anomaly_id']}: kind {anomaly['kind']}, severity {anomaly['severity']}, "
              f"rule {anomaly.get('rule_id')}, clip time {t_start} s to {t_end} s",
              f"- summary: {anomaly['summary']}",
              f"- data is {'SIMULATED (a labelled test fault)' if simulated else 'from real footage'}"]
    for c in checks:
        lines.append(f"- check:{c['check_id']}: {c['rule_id']} {c['outcome']}; expected {c['expected']}, "
                     f"observed {c['observed']}, residual {c['residual_pct']} %, tolerance {c['tolerance_used']} %")
    lines += [f"- frame:{f}" for f in anomaly["evidence"].get("frame_ids", [])[:5]]
    lines += await _coverage_lines(pool, clip_id, instruments)
    return "\n".join(lines)


async def build_clip_card(pool, clip_id: str) -> str:
    clip = dict(await pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", clip_id))
    lines, instruments = await _equipment_lines(pool, clip["equipment_id"])
    outcomes = await pool.fetch(
        "SELECT outcome, count(*) AS n FROM physics_checks WHERE clip_id = $1 GROUP BY outcome ORDER BY outcome", clip_id)
    anomalies = await pool.fetch(
        """SELECT a.anomaly_id, a.rule_id, a.severity, a.summary, d.output->>'headline' AS headline
           FROM anomalies a LEFT JOIN diagnoses d USING (anomaly_id) WHERE a.clip_id = $1 ORDER BY a.ts_start""", clip_id)
    corrections = await pool.fetch(
        """SELECT pc.check_id, pc.rule_id, pc.frame_id, i.physics_role, r.raw_text, pc.relook
           FROM physics_checks pc JOIN readings r ON r.reading_id = pc.suspect_reading_id
           JOIN instruments i ON i.instrument_id = r.instrument_id
           WHERE pc.clip_id = $1 AND pc.outcome = 'misread_fixed' ORDER BY pc.ts LIMIT 12""", clip_id)
    lines += ["\n## This clip",
              f"- clip {clip_id}: status {clip['status']}, "
              f"data is {'SIMULATED (a labelled test)' if clip.get('is_demo_replay') else 'from real footage'}",
              "- physics check outcomes: " + (", ".join(f"{r['outcome']} {r['n']}" for r in outcomes) or "none yet")]
    for a in anomalies:
        lines.append(f"- anomaly:{a['anomaly_id']}: {a['rule_id']} ({a['severity']}): {a['headline'] or a['summary']}")
    for c in corrections:
        fixed = next((x.get("raw_text") for x in (c["relook"] or {}).get("attempts", []) if x.get("outcome") == "fixed"), None)
        lines.append(f"- check:{c['check_id']}: {c['physics_role']} first read \"{c['raw_text']}\", re-look read "
                     f"\"{fixed}\" ({c['rule_id']} failed, then passed), frame:{c['frame_id']}")
    lines += await _coverage_lines(pool, clip_id, instruments)
    return "\n".join(lines)
