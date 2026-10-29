"""Builds a test video from the mock panel, with exact ground truth.

Each state is a seeded, frozen render of the mock panel (cycling BATT -> OUTPUT -> AC OUT pages), held
for --hold seconds, so the pipeline sees unchanged frames (reuse) and page changes (re-read).

    python -m eval.synth.make_mock_clip --states 9 --hold 1.5 --ghost faint --name mock_day1
Writes data/mock_clips/<name>.mp4 (1400x820) and <name>_truth.csv (readings.csv format, one row per
displayed field per state, t_seconds = state start). SYNTHETIC: development only.
"""

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

from backend.app.config import PROJECT_ROOT
from eval.spikes.mock_spike import PAGES, capture, truth_from_dom

OUT_DIR = PROJECT_ROOT / "data" / "mock_clips"
ROLES = {  # (page, unit) -> physics role, matching the mock panel and backend/app/seed.py
    ("BATT", "V"): "battery_v", ("BATT", "%"): "battery_soc_pct",
    ("OUTPUT", "W"): "output_w", ("OUTPUT", "VA"): "output_va", ("OUTPUT", "%"): "load_pct",
    ("AC OUT", "V"): "ac_v", ("AC OUT", "Hz"): "ac_hz",
}


def build(name: str, states: int, hold_s: float, ghost: str, first_seed: int, fps: int = 10,
          coherent: bool = False, fault: bool = False) -> tuple[Path, Path]:
    """coherent=False: every state is a new random panel (reading tests).
    coherent=True: one physical state (same seed) shown page after page, so values on different pages
    obey the same physics (physics-engine tests)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    video_path, truth_path = OUT_DIR / f"{name}.mp4", OUT_DIR / f"{name}_truth.csv"
    writer = None
    rows = []
    for n in range(states):
        page = n % len(PAGES)
        seed = first_seed if coherent else first_seed + n
        shot, dom = capture(seed, page, ghost, fault)
        truth = truth_from_dom(dom)
        frame = cv2.imdecode(np.frombuffer(shot.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if writer is None:
            writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps,
                                     (frame.shape[1], frame.shape[0]))
        for _ in range(max(1, round(hold_s * fps))):
            writer.write(frame)
        t = round(n * hold_s, 3)
        for text, unit in truth["inverter"]:
            rows.append([name, t, "inverter_main", ROLES.get((PAGES[page], unit), f"unknown_{unit}"), text, unit,
                         "mock_panel", "generator", f"page={PAGES[page]};seed={seed};ghost={ghost};simulated=true"])
        for text, unit in truth["clamp"]:
            rows.append([name, t, "clamp_dc", "battery_i", text, unit, "mock_panel", "generator",
                         f"seed={seed};ghost={ghost};simulated=true"])
        print(f"  state {n + 1}/{states}: page {PAGES[page]:<6} {truth}")
    writer.release()
    with open(truth_path, "w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(["clip_id", "t_seconds", "instrument_label", "field", "true_value", "unit", "source", "labeller", "notes"])
        out.writerows(rows)
    return video_path, truth_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a mock-panel test video with ground truth.")
    parser.add_argument("--name", default="mock_day1")
    parser.add_argument("--states", type=int, default=9)
    parser.add_argument("--hold", type=float, default=1.5, help="seconds each panel state is held")
    parser.add_argument("--ghost", default="faint", choices=["off", "faint", "strong"])
    parser.add_argument("--first-seed", type=int, default=101)
    parser.add_argument("--fault", action="store_true", help="SIMULATED hidden 250 W DC load (needs --coherent)")
    parser.add_argument("--coherent", action="store_true",
                        help="one physical state shown page after page (values obey the same physics)")
    args = parser.parse_args()
    video, truth = build(args.name, args.states, args.hold, args.ghost, args.first_seed, coherent=args.coherent,
                         fault=args.fault)
    print(f"video: {video}\ntruth: {truth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
