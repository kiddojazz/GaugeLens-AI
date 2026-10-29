"""Creates the schema. Run once (safe to re-run):

    python -m backend.app.db.init_db

Uses TimescaleDB when the extension can be created (local Docker image; Azure only if allow-listed),
otherwise plain PostgreSQL. The application never depends on Timescale-only features.
"""

import asyncio
import sys
from pathlib import Path

import asyncpg

from ..config import get_settings
from .pool import clean_dsn

SCHEMA = Path(__file__).with_name("schema.sql")


async def init(database_url: str) -> bool:
    conn = await asyncpg.connect(clean_dsn(database_url), timeout=10)
    try:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            timescale = True
        except asyncpg.PostgresError as exc:
            print(f"TimescaleDB not available ({str(exc).splitlines()[0]}); using plain PostgreSQL.")
            timescale = False
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        if timescale:
            await conn.execute("SELECT create_hypertable('readings', 'ts', if_not_exists => TRUE, migrate_data => TRUE)")
        return timescale
    finally:
        await conn.close()


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set.")
        return 1
    timescale = asyncio.run(init(settings.database_url))
    print(f"Schema ready ({'TimescaleDB hypertable on readings' if timescale else 'plain PostgreSQL'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
