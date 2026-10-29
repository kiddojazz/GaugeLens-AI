"""asyncpg connection pool. JSON/JSONB columns map to Python dicts and lists."""

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

# libpq options asyncpg doesn't understand (it would send them to the server as settings and fail);
# hosted Postgres connection strings (e.g. Neon's) include them.
_LIBPQ_ONLY = {"channel_binding", "gssencmode", "target_session_attrs"}


def clean_dsn(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in _LIBPQ_ONLY]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _init_connection(conn: asyncpg.Connection) -> None:
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(database_url: str, min_size: int = 1, max_size: int = 8) -> asyncpg.Pool:
    return await asyncpg.create_pool(clean_dsn(database_url), min_size=min_size, max_size=max_size,
                                     init=_init_connection, command_timeout=60)
