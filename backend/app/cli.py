"""GaugeLens command line (day-0 spikes and quick checks).

    python -m backend.app.cli check                         # which settings are missing
    python -m backend.app.cli ping                          # Spike A: can we reach the model?
    python -m backend.app.cli read photo.jpg                # Spike B: read one display
    python -m backend.app.cli read frames/ --context "inverter, 24 V, 3.5 kVA"
    python -m backend.app.cli read photo.jpg --box 410,220,380,160    # crop x,y,w,h first
    python -m backend.app.cli process data/mock_clips/mock_day1.mp4 --equipment inv_mock \\
        --truth data/mock_clips/mock_day1_truth.csv       # day-1 gate: clip -> readings in the DB
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT, get_settings, missing_settings
from .llm import CacheMiss, ChatClient, ModelUnavailable, StructuredOutputError
from .pipeline.read_digital import PROMPT_VERSION, read_display

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
RESULTS_DIR = PROJECT_ROOT / "eval" / "spikes" / "results"


def _images(paths: list[str]) -> list[Path]:
    found = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found += sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        elif path.is_file():
            found.append(path)
        else:
            print(f"  ! not found: {path}", file=sys.stderr)
    return found


def _box(text: str | None):
    if not text:
        return None
    parts = [int(p) for p in text.split(",")]
    if len(parts) != 4:
        raise SystemExit("--box must be x,y,w,h")
    return tuple(parts)


def cmd_check(settings) -> int:
    missing = missing_settings(settings)
    print(f"model: {settings.read_model} (Meta Model API) | cache_only: {settings.cache_only}")
    if missing:
        print("missing: " + ", ".join(missing))
        return 1
    print("settings OK")
    return 0


async def cmd_inventory(settings, args) -> int:
    from .pipeline.read_equipment import read_inventory, suggest_registry

    client = ChatClient(settings)
    images = _images(args.paths)
    if not images:
        print("No images given.")
        return 1
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"inventory_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    failures = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for image in images:
            record = {"image": str(image), "model": settings.read_model}
            try:
                inventory, meta = await read_inventory(client, image.read_bytes(), context=args.context)
            except (ModelUnavailable, StructuredOutputError, CacheMiss) as exc:
                failures += 1
                record["error"] = str(exc)
                print(f"\n{image.name}: FAILED {exc}")
            else:
                suggestions = suggest_registry(inventory)
                record.update(inventory=inventory.model_dump(), suggestions=suggestions, latency_ms=meta.latency_ms)
                print(f"\n{image.name}  [{inventory.scene}]  ({'cache' if meta.cache_hit else f'{meta.latency_ms} ms'})")
                for item in inventory.items:
                    print(f"  - {item.kind}{f' ({item.label})' if item.label else ''}")
                    for f in item.nameplate:
                        print(f"      {f.name:<22} {f.value} {f.unit or ''}{'' if f.legible else '   (unclear)'}")
                    for r in item.readings:
                        print(f"      reading: {r.label} = {r.raw_text} {r.unit or ''}{'' if r.legible else '   (unclear)'}")
                for s in suggestions:
                    label = f" ({s['label']})" if s["label"] else ""
                    print(f"  => registry suggestion for {s['kind']}{label}: {s['params']}")
                if inventory.privacy_flags:
                    print(f"  ! blur before sharing: {'; '.join(inventory.privacy_flags)}")
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\n{len(images) - failures}/{len(images)} read - results: {out_path}")
    return 1 if failures else 0


async def cmd_ping(settings) -> int:
    client = ChatClient(settings)
    try:
        reply, latency = await client.ping()
    except ModelUnavailable as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"{settings.read_model} replied {reply!r} in {latency} ms")
    return 0


async def cmd_read(settings, args) -> int:
    client = ChatClient(settings)
    images = _images(args.paths)
    if not images:
        print("No images given.")
        return 1
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else RESULTS_DIR / f"read_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    failures = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for image in images:
            record = {"image": str(image), "provider": "meta", "model": settings.read_model,
                      "prompt_version": PROMPT_VERSION, "context": args.context, "box": args.box}
            try:
                reading, meta = await read_display(client, image.read_bytes(), context=args.context, box=_box(args.box))
            except (ModelUnavailable, StructuredOutputError, CacheMiss) as exc:
                failures += 1
                record["error"] = str(exc)
                print(f"\n{image.name}: FAILED {exc}")
            else:
                record.update(reading=reading.model_dump(), latency_ms=meta.latency_ms, cache_hit=meta.cache_hit,
                              input_tokens=meta.input_tokens, output_tokens=meta.output_tokens, attempts=meta.attempts)
                source = "cache" if meta.cache_hit else f"{meta.latency_ms} ms, {meta.input_tokens}+{meta.output_tokens} tokens"
                print(f"\n{image.name}  [{reading.display_kind}, page={reading.page_hint}, "
                      f"glare={reading.glare_or_occlusion}]  ({source})")
                for f in reading.fields:
                    shown = f"{f.raw_text} {f.unit or ''}".strip()
                    flag = "" if f.legible else f"   ILLEGIBLE: {f.reason_if_illegible}"
                    print(f"   {f.label:<18} {shown:<14}{flag}")
                if reading.notes:
                    print(f"   note: {reading.notes}")
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\n{len(images) - failures}/{len(images)} read - results: {out_path}")
    return 1 if failures else 0


def _truth_windows(path: str) -> list[tuple[float, dict]]:
    """[(state start t, {physics_role: true value})] from a readings.csv-format truth file."""
    import csv

    states: dict[float, dict] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            states.setdefault(float(row["t_seconds"]), {})[row["field"]] = float(row["true_value"])
    return sorted(states.items())


def score_against_truth(readings: list[dict], truth_path: str) -> dict:
    """Each stored reading vs the value displayed at its frame time. 'not_displayed' = a value was
    mapped to an instrument that wasn't on screen (a mapping error)."""
    windows = _truth_windows(truth_path)
    counts = {"exact": 0, "wrong": 0, "not_displayed": 0, "no_value": 0}
    wrong = []
    for r in readings:
        state = next((values for start, values in reversed(windows) if float(r["t_offset_s"]) + 1e-6 >= start), None)
        if r["value"] is None:
            counts["no_value"] += 1
        elif state is None or r["physics_role"] not in state:
            counts["not_displayed"] += 1
            wrong.append(f"t={r['t_offset_s']} {r['physics_role']}={r['value']} (not on screen)")
        elif abs(state[r["physics_role"]] - r["value"]) < 1e-9:
            counts["exact"] += 1
        else:
            counts["wrong"] += 1
            wrong.append(f"t={r['t_offset_s']} {r['physics_role']}: read {r['value']}, shown {state[r['physics_role']]}")
    return {"counts": counts, "examples": wrong[:10]}


async def cmd_process(settings, args) -> int:
    from .db.pool import create_pool
    from .evidence.store import make_evidence_store
    from .jobs.queue import PostgresJobQueue
    from .jobs.worker import Worker
    from .pipeline.clips import create_clip

    if not settings.database_url:
        print("DATABASE_URL is not set.")
        return 1
    video = Path(args.video)
    pool = await create_pool(settings.database_url)
    try:
        evidence = make_evidence_store(settings)
        queue = PostgresJobQueue(pool)
        clip_id, job_id = await create_clip(pool, evidence, queue, video=video.read_bytes(), filename=video.name,
                                            equipment_id=args.equipment, channel="seed",
                                            is_demo_replay=args.simulated)
        print(f"clip {clip_id} queued as {job_id}; processing...")
        worker = Worker(pool, settings, evidence=evidence)
        while (await queue.get(job_id))["status"] in ("queued", "running"):
            if await worker.run_once() is None:
                await asyncio.sleep(1)                      # job is in backoff after a failure
        job = await queue.get(job_id)
        done = await pool.fetchval(
            "SELECT payload FROM clip_events WHERE clip_id = $1 AND type = 'clip_done' ORDER BY event_id DESC LIMIT 1",
            clip_id)
        print(f"job {job['status']} after {job['attempts']} attempt(s); {done or job['last_error']}")
        readings = [dict(r) for r in await pool.fetch(
            """SELECT f.t_offset_s, i.physics_role, r.value, r.raw_text, r.status, r.reused_from IS NOT NULL AS reused
               FROM readings r JOIN frames f ON f.frame_id = r.frame_id JOIN instruments i USING (instrument_id)
               WHERE f.clip_id = $1 ORDER BY f.t_offset_s, i.physics_role""", clip_id)]
        calls = await pool.fetchrow(
            """SELECT count(*) AS n, count(*) FILTER (WHERE cache_hit) AS cached, sum(input_tokens) AS tin,
                      sum(output_tokens) AS tout, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50
               FROM model_calls WHERE call_id IN (SELECT DISTINCT model_call_id FROM readings r
                    JOIN frames f ON f.frame_id = r.frame_id WHERE f.clip_id = $1)""", clip_id)
        print(f"{len(readings)} readings stored ({sum(r['reused'] for r in readings)} reused from unchanged crops); "
              f"{calls['n']} model calls ({calls['cached']} cached), p50 {calls['p50']} ms, "
              f"tokens {calls['tin']}+{calls['tout']}")
        checks = await pool.fetch(
            """SELECT rule_id, outcome, count(*) AS n FROM physics_checks WHERE clip_id = $1
               GROUP BY rule_id, outcome ORDER BY rule_id, outcome""", clip_id)
        if checks:
            print("physics checks:")
            for c in checks:
                print(f"   {c['rule_id']:<34} {c['outcome']:<13} x{c['n']}")
            for f in await pool.fetch(
                    """SELECT pc.rule_id, pc.residual_pct, pc.candidate_fix, i.physics_role, r.value, f.t_offset_s
                       FROM physics_checks pc LEFT JOIN readings r ON r.reading_id = pc.suspect_reading_id
                       LEFT JOIN instruments i ON i.instrument_id = r.instrument_id
                       LEFT JOIN frames f ON f.frame_id = pc.frame_id
                       WHERE pc.clip_id = $1 AND pc.outcome = 'needs_review' ORDER BY pc.ts LIMIT 5""", clip_id):
                fix = f["candidate_fix"]
                print(f"   ! t={f['t_offset_s']} {f['rule_id']}: suspect {f['physics_role']}={f['value']}"
                      + (f", candidate fix {fix['value']:g} ({fix['kind']})" if fix else ""))
        if args.truth:
            result = score_against_truth(readings, args.truth)
            print(f"vs truth: {result['counts']}")
            for line in result["examples"]:
                print(f"   {line}")

        # Diagnosis jobs this clip queued (one per confirmed anomaly)
        while await pool.fetchval(
                """SELECT count(*) FROM jobs WHERE kind = 'diagnose' AND payload->>'clip_id' = $1
                   AND status IN ('queued', 'running')""", clip_id):
            if await worker.run_once() is None:
                await asyncio.sleep(1)
        for a in await pool.fetch(
                """SELECT a.anomaly_id, a.rule_id, a.severity, a.is_simulated, d.verified, d.model, d.output
                   FROM anomalies a LEFT JOIN diagnoses d USING (anomaly_id) WHERE a.clip_id = $1""", clip_id):
            out = a["output"] or {}
            print(f"\nANOMALY {a['anomaly_id']} ({a['rule_id']}, {a['severity']}{', SIMULATED' if a['is_simulated'] else ''})")
            if not out:
                print("   no diagnosis")
                continue
            print(f"   diagnosis: verified={a['verified']} model={a['model']} tools={[t['tool'] for t in out.get('trace', [])]}")
            print(f"   headline: {out['headline']}")
            for o in out["observations"]:
                print(f"   - {o['statement']}  {o['evidence']}")
            for h in out["hypotheses"]:
                print(f"   ? {h['cause']} ({h['likelihood']})")
            print(f"   unknowns: {out['unknowns']}")
            print(f"   action: {out['proposed_action']}\n   safety: {out['safety_note']}\n   confidence: {out['confidence']}")
            if out.get("citation_problems"):
                print(f"   citation problems: {out['citation_problems']}")
        return 0 if job["status"] == "done" else 1
    finally:
        await pool.close()


async def cmd_eval(settings, args) -> int:
    """Runs the agent evaluation in this process and prints the scores (uses model credits)."""
    from types import SimpleNamespace

    from .agent.evals import BY_ID, run_eval
    from .db.pool import create_pool
    from .evidence.store import make_evidence_store
    from .llm import ChatClient

    unknown = [s for s in args.scenario or [] if s not in BY_ID]
    if unknown:
        print(f"Unknown scenarios: {', '.join(unknown)}. Known: {', '.join(BY_ID)}")
        return 1
    pool = await create_pool(settings.database_url)
    try:
        ctx = SimpleNamespace(pool=pool, client=ChatClient(settings), evidence=make_evidence_store(settings))
        eval_run_id = await run_eval(ctx, scenario_ids=args.scenario or None)
        run = await pool.fetchrow("SELECT * FROM eval_runs WHERE eval_run_id = $1", eval_run_id)
        print(f"evaluation {eval_run_id}: {run['passed']}/{run['scenarios']} scenarios passed, score {run['score']:.2f}")
        for r in await pool.fetch("SELECT * FROM eval_results WHERE eval_run_id = $1 ORDER BY scenario_id", eval_run_id):
            failed = [k for k, v in r["criteria"].items() if not v["pass"]]
            print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['scenario_id']:<18} {r['score']:.2f}"
                  + (f"  failed: {', '.join(failed)}" if failed else ""))
        return 0
    finally:
        await pool.close()


async def cmd_whatsapp_check(settings, args) -> int:
    """Checks the WhatsApp settings against Meta (token never printed); --to sends the hello_world template."""
    import httpx

    missing = [k for k in ("whatsapp_access_token", "whatsapp_phone_number_id", "webhook_verify_token")
               if not getattr(settings, k)]
    print("settings: " + ("all set" if not missing else "missing " + ", ".join(m.upper() for m in missing)))
    print(f"app secret: {'set (webhook signatures checked)' if settings.meta_app_secret else 'NOT set (signatures not checked)'}")
    if missing:
        return 1
    base = f"https://graph.facebook.com/{settings.graph_version}"
    auth = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(f"{base}/{settings.whatsapp_phone_number_id}", headers=auth,
                           params={"fields": "display_phone_number,verified_name,quality_rating"})
        if r.status_code != 200:
            error = r.json().get("error", {})
            print(f"token check FAILED: {error.get('code')} {error.get('message')}")
            if error.get("code") == 190:
                print("  -> the access token is wrong or expired: generate a new one in API Setup and update .env")
            return 1
        info = r.json()
        print(f"token OK: sending from {info.get('display_phone_number')} ({info.get('verified_name')})")
        if args.to:
            r = await http.post(f"{base}/{settings.whatsapp_phone_number_id}/messages", headers=auth, json={
                "messaging_product": "whatsapp", "to": args.to.lstrip("+"), "type": "template",
                "template": {"name": "hello_world", "language": {"code": "en_US"}}})
            body = r.json()
            if r.status_code != 200:
                print(f"send FAILED: {body.get('error', {}).get('code')} {body.get('error', {}).get('message')}")
                return 1
            print(f"hello_world accepted by Meta (id {body['messages'][0]['id']}). Delivery status arrives on the webhook.")
    return 0


def cmd_evidence_sync(settings, args) -> int:
    """Uploads local evidence files the blob container doesn't have yet (same URIs, so DB rows don't change)."""
    from .evidence.store import BlobEvidenceStore

    if not settings.azure_storage_connection_string:
        print("AZURE_STORAGE_CONNECTION_STRING is not set.")
        return 1
    store = BlobEvidenceStore.from_connection_string(settings.azure_storage_connection_string,
                                                     settings.azure_blob_container, settings.evidence_dir)
    present = {b.name: b.size for b in store.container.list_blobs()}
    files = sorted(p for p in settings.evidence_dir.rglob("*") if p.is_file())
    uploaded = skipped = 0
    for path in files:
        uri = path.relative_to(settings.evidence_dir).as_posix()
        if present.get(uri) == path.stat().st_size:
            skipped += 1
            continue
        if args.dry_run:
            print(f"would upload {uri}")
        else:
            store.upload(uri, path.read_bytes())
        uploaded += 1
        if uploaded % 50 == 0:
            print(f"  {uploaded} uploaded...")
    verb = "would upload" if args.dry_run else "uploaded"
    print(f"{verb} {uploaded}, already there {skipped}, container '{settings.azure_blob_container}'")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GaugeLens day-0 tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Show which settings are missing.")
    sub.add_parser("ping", help="Spike A: one tiny model call.")
    read = sub.add_parser("read", help="Spike B: read display image(s).")
    read.add_argument("paths", nargs="+", help="Image files or folders.")
    read.add_argument("--context", default="", help="Equipment description. Never put expected values here.")
    read.add_argument("--box", help="Crop x,y,w,h (pixels) before reading.")
    read.add_argument("--out", help="JSONL output path (default eval/spikes/results/read_<time>.jsonl).")
    inventory = sub.add_parser("inventory", help="Nameplate / panel / HMI photo(s) -> equipment + registry suggestions.")
    inventory.add_argument("paths", nargs="+", help="Image files or folders.")
    inventory.add_argument("--context", default="", help="Site description, e.g. '11/0.415 kV distribution substation'.")
    process = sub.add_parser("process", help="Day-1 gate: upload a clip, run the worker, score vs truth.")
    process.add_argument("video")
    process.add_argument("--equipment", required=True, help="equipment_id, e.g. inv_mock")
    process.add_argument("--truth", help="readings.csv-format ground truth to score against")
    process.add_argument("--simulated", action="store_true", help="label the clip as a SIMULATED (demo) fault")
    ev = sub.add_parser("eval", help="Run the agent evaluation scenarios (uses model credits).")
    ev.add_argument("--scenario", action="append", help="only this scenario (repeatable)")
    wac = sub.add_parser("whatsapp-check", help="Check the WhatsApp token and number (optionally send hello_world).")
    wac.add_argument("--to", help="also send the hello_world template to this number, e.g. 2348012345678")
    sync = sub.add_parser("evidence-sync", help="Upload local evidence/ files to the Azure Blob container.")
    sync.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):        # Windows consoles default to cp1252 ("≤", "√" would crash)
        stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    if args.command == "check":
        return cmd_check(settings)
    if args.command == "whatsapp-check":
        return asyncio.run(cmd_whatsapp_check(settings, args))
    if args.command == "evidence-sync":
        return cmd_evidence_sync(settings, args)
    missing = missing_settings(settings)
    if missing:
        print("Missing settings: " + ", ".join(missing) + " (copy .env.example to .env and fill them in)")
        return 1
    if args.command == "ping":
        return asyncio.run(cmd_ping(settings))
    if args.command == "process":
        return asyncio.run(cmd_process(settings, args))
    if args.command == "eval":
        return asyncio.run(cmd_eval(settings, args))
    if args.command == "inventory":
        return asyncio.run(cmd_inventory(settings, args))
    return asyncio.run(cmd_read(settings, args))


if __name__ == "__main__":
    sys.exit(main())
