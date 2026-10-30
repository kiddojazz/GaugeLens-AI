"""Copies this database's data into another one (e.g. the hosted Postgres for the Azure demo), so the hosted app
starts with everything already processed: clips, readings, corrections, anomalies, diagnoses, traces, evals, chats.
Evidence files are not copied here: they live in Blob Storage (cli evidence-sync).

    python -m backend.app.db.copy --to-env AZURE_DATABASE_URL            # target must be empty
    python -m backend.app.db.copy --to-env AZURE_DATABASE_URL --replace  # wipe the target's data first

The target gets the schema first (init_db). Jobs and the SSE event log are not copied: they are runtime state.

Moving the hosted database to another server copies everything, accounts and runtime state included:

    python -m backend.app.db.copy --from-env OLD_URL --to-env NEW_URL
"""

import argparse
import asyncio
import os
import sys

import asyncpg

from ..config import get_settings, load_dotenv
from .init_db import init
from .pool import clean_dsn

# parents before children (foreign keys); equipment rows that measure another unit come after it
TABLES = ["sites", "equipment", "displays", "instruments", "clips", "clip_rois", "clip_annotations", "frames",
          "readings", "physics_checks", "anomalies", "diagnoses", "model_calls", "agent_runs", "agent_spans",
          "eval_runs", "eval_results", "chat_messages"]
# only with --from-env: a local refresh must not overwrite the hosted accounts with test ones
MOVE_ONLY = ["users", "sessions", "jobs", "clip_events"]
ORDER = {"equipment": "linked_equipment_id NULLS FIRST", "agent_spans": "run_id, seq"}


async def _columns(conn, table: str) -> list[str]:
    return [r["column_name"] for r in await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1 "
        "ORDER BY ordinal_position", table)]


async def copy(source_url: str, target_url: str, replace: bool, move: bool = False) -> dict[str, int]:
    tables = MOVE_ONLY[:2] + TABLES + MOVE_ONLY[2:] if move else TABLES
    await init(target_url)
    src = await asyncpg.connect(clean_dsn(source_url))
    dst = await asyncpg.connect(clean_dsn(target_url), timeout=30)
    counts = {}
    try:
        present = {t for t in tables if await _columns(dst, t)}
        existing = sum([await dst.fetchval(f"SELECT count(*) FROM {t}") for t in present])
        if existing and not replace:
            raise SystemExit(f"the target already has {existing} rows; use --replace to overwrite them")
        async with dst.transaction():
            if replace:
                names = {*(t for t in tables if t in present), "jobs", "clip_events"}
                await dst.execute("TRUNCATE " + ", ".join(sorted(names)) + " CASCADE")
            for table in tables:
                if table not in present:
                    continue
                columns = [c for c in await _columns(src, table) if c in set(await _columns(dst, table))]
                order = f" ORDER BY {ORDER[table]}" if table in ORDER else ""
                rows = await src.fetch(f"SELECT {', '.join(columns)} FROM {table}{order}")
                if rows:
                    await dst.copy_records_to_table(table, records=[tuple(r) for r in rows], columns=columns)
                counts[table] = len(rows)
                print(f"  {table:<18} {len(rows):>7}", flush=True)
            if move:                                       # new events must not reuse the copied ids
                await dst.execute("SELECT setval(pg_get_serial_sequence('clip_events', 'event_id'), "
                                  "GREATEST((SELECT max(event_id) FROM clip_events), 1))")
    finally:
        await src.close()
        await dst.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to-env", required=True, help="name of the .env variable holding the target URL")
    parser.add_argument("--from-env", help="copy from this .env variable's database, accounts and jobs included "
                                           "(default: DATABASE_URL, data only)")
    parser.add_argument("--replace", action="store_true", help="delete the target's data first")
    args = parser.parse_args()
    load_dotenv()
    target = os.environ.get(args.to_env)
    source = os.environ.get(args.from_env) if args.from_env else get_settings().database_url
    if not target or not source:
        print(f"Need {args.from_env or 'DATABASE_URL'} (source) and {args.to_env} (target) in .env.")
        return 1
    if clean_dsn(target) == clean_dsn(source):
        print("Source and target are the same database.")
        return 1
    print("copying (values are never printed):")
    counts = asyncio.run(copy(source, target, args.replace, move=bool(args.from_env)))
    print(f"done: {sum(counts.values())} rows in {len(counts)} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
