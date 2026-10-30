"""Moving a database keeps accounts, sessions and the event log; a plain refresh leaves accounts alone."""

import asyncio
from urllib.parse import urlparse

import asyncpg

from backend.app.db.copy import copy
from backend.app.db.init_db import init


def test_a_move_copies_accounts_and_events_and_new_events_get_fresh_ids(clean_db):
    target = urlparse(clean_db)._replace(path="/gaugelens_test_copy").geturl()

    async def go():
        admin = await asyncpg.connect(clean_db)
        await admin.execute("DROP DATABASE IF EXISTS gaugelens_test_copy")
        await admin.execute("CREATE DATABASE gaugelens_test_copy")
        await admin.execute("INSERT INTO users (user_id, email, password_hash) VALUES ('usr_1', 'a@b.co', 'h')")
        await admin.execute("INSERT INTO sessions (session_id, token_hash, kind, user_id) VALUES ('s1', 't', 'user', 'usr_1')")
        await admin.execute("INSERT INTO clip_events (event_id, clip_id, type, payload) VALUES (41, 'c', 'x', '{}')")
        await admin.close()
        await init(target)
        await copy(clean_db, target, replace=False)
        dst = await asyncpg.connect(target)
        assert await dst.fetchval("SELECT count(*) FROM users") == 0        # a plain copy leaves accounts out
        await dst.close()
        await copy(clean_db, target, replace=True, move=True)
        dst = await asyncpg.connect(target)
        assert await dst.fetchval("SELECT user_id FROM sessions") == "usr_1"
        new_id = await dst.fetchval("INSERT INTO clip_events (clip_id, type, payload) VALUES ('c', 'y', '{}') RETURNING event_id")
        assert new_id == 42
        await dst.close()
        admin = await asyncpg.connect(clean_db)
        await admin.execute("DROP DATABASE gaugelens_test_copy WITH (FORCE)")
        await admin.close()

    asyncio.run(go())
