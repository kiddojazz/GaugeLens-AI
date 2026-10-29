"""Database fixtures. Integration tests use a separate database, gaugelens_test, on the server in
DATABASE_URL (created on first use, wiped between tests). Without a reachable server they are skipped;
the main gaugelens database is never touched."""

import asyncio
import os
from urllib.parse import urlparse

import asyncpg
import pytest

from backend.app.config import load_dotenv
from backend.app.db.init_db import init

TEST_DB = "gaugelens_test"
TABLES = ("channel_messages, chat_messages, eval_results, eval_runs, agent_spans, agent_runs, clip_events, jobs, model_calls, diagnoses, anomalies, physics_checks, readings, frames, "
          "clip_annotations, clips, instruments, displays, equipment, sites")


@pytest.fixture(scope="session")
def db_url():
    load_dotenv()
    admin_url = os.environ.get("DATABASE_URL")
    if not admin_url:
        pytest.skip("DATABASE_URL not set; integration tests skipped")
    test_url = urlparse(admin_url)._replace(path=f"/{TEST_DB}").geturl()

    async def prepare() -> str | None:
        try:
            conn = await asyncpg.connect(admin_url, timeout=3)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        try:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB):
                await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        finally:
            await conn.close()
        await init(test_url)
        return None

    error = asyncio.run(prepare())
    if error:
        pytest.skip(f"database not reachable ({error}); integration tests skipped")
    return test_url


@pytest.fixture
def clean_db(db_url):
    async def wipe():
        conn = await asyncpg.connect(db_url)
        try:
            await conn.execute(f"TRUNCATE {TABLES} CASCADE")
        finally:
            await conn.close()

    asyncio.run(wipe())
    return db_url
