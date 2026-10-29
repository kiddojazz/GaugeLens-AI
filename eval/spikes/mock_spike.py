"""Spike B on the mock panel, scored automatically against ground truth.

For each (seed, page, ghost level): headless Edge takes a screenshot of the mock panel, a second headless
run with the same URL dumps the DOM (same seed -> same values = ground truth), then the production reader
reads the inverter LCD and the clamp meter crops. Exact match = same number as displayed.

    python -m eval.spikes.mock_spike --seeds 1 2 --ghosts off faint strong
Results: eval/spikes/results/mock_spike_<time>.jsonl + a summary printed per ghost level.
SYNTHETIC data: use it to tune prompts, never as headline evaluation numbers.
"""

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from backend.app.config import PROJECT_ROOT, get_settings, missing_settings
from backend.app.llm import ChatClient, ModelUnavailable, StructuredOutputError
from backend.app.pipeline.read_digital import PROMPT_VERSION, read_display

PANEL = PROJECT_ROOT / "eval" / "synth" / "mock_panel" / "index.html"
OUT_DIR = PROJECT_ROOT / "eval" / "spikes" / "results"
SHOTS = PROJECT_ROOT / "eval" / "spikes" / "screens" / "mock_spike"
EDGE_PATHS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
WINDOW = "1400,820"                           # crop boxes below assume this window with controls visible
BOXES = {"inverter": (245, 238, 640, 255), "clamp": (975, 302, 186, 108)}
CONTEXT = {"inverter": "off-grid inverter display", "clamp": "DC clamp meter display"}
PAGES = ["BATT", "OUTPUT", "AC OUT"]


def browser() -> str:
    for path in EDGE_PATHS:
        if Path(path).is_file():
            return path
    raise SystemExit("Edge or Chrome not found.")


def _url(seed: int, page: int, ghost: str, fault: bool = False) -> str:
    return f"{PANEL.as_uri()}?seed={seed}&page={page}&ghost={ghost}" + ("&fault=1" if fault else "")


def _wait_for(check, timeout_s: float = 45.0) -> bool:
    """Edge's launcher can exit before its child process finishes writing; poll for the result."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.25)
    return False


def capture(seed: int, page: int, ghost: str, fault: bool = False) -> tuple[Path, str]:
    """Screenshot + DOM of the same deterministic panel state (fault=True: the SIMULATED hidden DC load)."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot = SHOTS / f"seed{seed}_p{page}_{ghost}{'_fault' if fault else ''}.png"
    shot.unlink(missing_ok=True)
    common = ["--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={WINDOW}",
              "--virtual-time-budget=4000"]
    work = Path(tempfile.mkdtemp(prefix="gl-edge-"))
    subprocess.run([browser(), *common, f"--user-data-dir={work / 'shot'}", f"--screenshot={shot}",
                    _url(seed, page, ghost, fault)], capture_output=True, timeout=60)
    if not _wait_for(lambda: shot.is_file() and shot.stat().st_size > 0):
        raise RuntimeError(f"no screenshot for seed={seed} page={page} ghost={ghost}")

    # Edge's --dump-dom output never reaches a pipe or a Python-redirected file on Windows (its child
    # process doesn't inherit them), but PowerShell's Start-Process redirection works.
    dom_file = work / "dom.html"
    args = ", ".join(f"'{a}'" for a in [*common, f"--user-data-dir={work / 'dom'}", "--dump-dom", _url(seed, page, ghost, fault)])
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process -FilePath '{browser()}' -ArgumentList @({args}) -Wait -NoNewWindow "
                    f"-RedirectStandardOutput '{dom_file}' -RedirectStandardError '{work / 'dom.err'}'"],
                   capture_output=True, timeout=90)
    if not _wait_for(lambda: dom_file.is_file() and b"</html>" in dom_file.read_bytes()):
        raise RuntimeError(f"no DOM for seed={seed} page={page} ghost={ghost}")
    dom = dom_file.read_bytes().decode("utf-8", errors="replace")
    shutil.rmtree(work, ignore_errors=True)          # best effort; Edge may still hold the profile briefly
    return shot, dom


def truth_from_dom(dom: str) -> dict:
    """{'inverter': [(text, unit), ...], 'clamp': [(text, 'A')]} as displayed."""
    def live(element_id: str) -> str:
        m = re.search(rf'id="{element_id}"[^>]*>.*?<span class="live">([^<]*)</span>', dom, re.S)
        return (m.group(1) if m else "").replace("!", "")

    def unit(element_id: str) -> str:
        m = re.search(rf'id="{element_id}"[^>]*>([^<]*)<', dom)
        return m.group(1).strip() if m else ""

    inverter = [(live(v), unit(u)) for v, u in (("mainVal", "mainUnit"), ("sub1Val", "sub1Unit"), ("sub2Val", "sub2Unit"))]
    return {"inverter": [(t, u) for t, u in inverter if t], "clamp": [(live("clampVal"), "A")]}


def _num(text: str | None) -> float | None:
    """The number in a displayed/read string; units the model includes ('30%') are ignored."""
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else None


def score(truth: list, reading) -> list[dict]:
    """Per true field: exact | wrong | illegible | missing (matched by unit, case-insensitive)."""
    rows = []
    fields = list(reading.fields) if reading else []
    for text, unit in truth:
        match = next((f for f in fields if (f.unit or "").lower() == unit.lower()), None)
        if match is not None:
            fields.remove(match)
        if match is None:
            outcome, read = "missing", None
        elif not match.legible:
            outcome, read = "illegible", match.raw_text
        else:
            read = match.raw_text
            outcome = "exact" if _num(read) is not None and _num(read) == _num(text) else "wrong"
        rows.append({"unit": unit, "truth": text, "read": read, "outcome": outcome})
    return rows


async def run(seeds: list[int], ghosts: list[str]) -> int:
    settings = get_settings()
    missing = missing_settings(settings)
    if missing:
        print("Missing settings: " + ", ".join(missing))
        return 1
    client = ChatClient(settings)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"mock_spike_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    totals: dict = defaultdict(lambda: defaultdict(int))
    with open(out_path, "w", encoding="utf-8") as out:
        for ghost in ghosts:
            for seed in seeds:
                for page in range(len(PAGES)):
                    shot, dom = capture(seed, page, ghost)
                    truth = truth_from_dom(dom)
                    image = shot.read_bytes()
                    for display, box in BOXES.items():
                        try:
                            reading, meta = await read_display(client, image, context=CONTEXT[display], box=box)
                            info = {"latency_ms": meta.latency_ms, "cache_hit": meta.cache_hit,
                                    "tokens": [meta.input_tokens, meta.output_tokens]}
                        except (ModelUnavailable, StructuredOutputError) as exc:
                            reading, info = None, {"error": str(exc)[:300]}
                        rows = score(truth[display], reading)
                        for row in rows:
                            totals[ghost][row["outcome"]] += 1
                        shown = "  ".join(f"{r['truth']}{r['unit']}->{r['read']}({r['outcome']})" for r in rows)
                        print(f"{ghost:<6} seed={seed} {PAGES[page]:<6} {display:<8} {shown}")
                        out.write(json.dumps({"ghost": ghost, "seed": seed, "page": PAGES[page], "display": display,
                                              "prompt_version": PROMPT_VERSION, "model": settings.read_model,
                                              "screenshot": str(shot), "fields": rows,
                                              "reading": reading.model_dump() if reading else None, **info}) + "\n")
    print(f"\nprompt {PROMPT_VERSION} on {settings.read_model}")
    for ghost, counts in totals.items():
        n = sum(counts.values())
        print(f"  ghost={ghost:<6} exact {counts['exact']}/{n}  wrong {counts['wrong']}  "
              f"illegible {counts['illegible']}  missing {counts['missing']}")
    print(f"results: {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--ghosts", nargs="+", default=["off", "faint", "strong"], choices=["off", "faint", "strong"])
    args = parser.parse_args()
    return asyncio.run(run(args.seeds, args.ghosts))


if __name__ == "__main__":
    sys.exit(main())
