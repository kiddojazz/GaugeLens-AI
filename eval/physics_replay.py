"""Replays a ground-truth CSV (readings.csv format) through the physics engine, optionally injecting a misread.

    python -m eval.physics_replay data/mock_clips/mock_coherent_truth.csv
    python -m eval.physics_replay data/mock_clips/mock_coherent_truth.csv --inject battery_v=x2@3.0

--inject role=KIND@t replaces that role's value at the state starting at t with a misread
(x10, div10, x2, div2, or seg_<a>to<b> on the displayed digits). No model calls.
"""

import argparse
import csv
import sys

from backend.app.physics.engine import attribute, evaluate
from backend.app.physics.rules import load_pack
from backend.app.physics.state import RoleReading, RoleState
from backend.app.seed import MOCK

RESOLUTION = {role: res for role, _, _, _, _, res, _ in MOCK["instruments"]}
MOCK_INVERTER = {"nominal_v": 24, "rated_va": 3500, "idle_w": 35}


def misread(text: str, kind: str) -> str:
    value = float(text)
    if kind in ("x10", "div10", "x2", "div2"):
        factor = {"x10": 10, "div10": 0.1, "x2": 2, "div2": 0.5}[kind]
        return f"{value * factor:g}"
    a, b = kind.removeprefix("seg_").split("to")
    return text.replace(a, b, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("truth")
    parser.add_argument("--inject", help="role=KIND@t, e.g. battery_v=x2@3.0")
    args = parser.parse_args()
    inject = None
    if args.inject:
        role, rest = args.inject.split("=")
        kind, t = rest.split("@")
        inject = (role, kind, float(t))

    rows = list(csv.DictReader(open(args.truth, newline="", encoding="utf-8")))
    pack = load_pack("inverter")
    state = RoleState(steady_role=pack.steady.role, step_pct=pack.steady.step_pct)
    for t in sorted({float(r["t_seconds"]) for r in rows}):
        for r in (r for r in rows if float(r["t_seconds"]) == t):
            text = r["true_value"]
            if inject and r["field"] == inject[0] and t == inject[2]:
                text = misread(text, inject[1])
                print(f"  injected: {r['field']} {r['true_value']} -> {text} ({inject[1]})")
            state.update(RoleReading(role=r["field"], value=float(text), ts=t, reading_id=f"{r['field']}@{t:g}",
                                     resolution=RESOLUTION.get(r["field"]), raw_text=text, confidence=0.8))
        results = evaluate(pack, state, MOCK_INVERTER, now=t)
        ran = [x for x in results if x.status != "skipped"]
        print(f"t={t:>5.1f}  " + "  ".join(
            f"{x.rule_id.split('.')[1]}={x.status}" + (f"({x.residual_pct:+.1f}%)" if x.residual_pct is not None else "")
            for x in ran))
        if any(x.status == "fail" for x in results):
            a = attribute(pack, state, MOCK_INVERTER, results, now=t)
            print(f"         -> suspect {a.suspect_reading_id}: {a.reason}"
                  + (f"; candidate fix {a.candidate_fix['value']:g}" if a.candidate_fix else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
