"""Scripted fake model client and test settings (no network, no Azure, no database)."""

import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.config import Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        meta_api_key="test-key", meta_base_url="https://api.meta.ai/v1", meta_model_read="muse-spark-1.3",
        temperature=None, structured_mode="tool", tool_choice="forced",       # the tool-call path is the one most
        cache_dir=tmp_path / "cache", database_url=None)                    # tests script (json_schema: see below)
    values.update(overrides)
    return Settings(**values)


def tool_response(arguments, name: str = "record_display_reading", prompt_tokens: int = 1200, completion_tokens: int = 80):
    """A chat.completions response carrying one tool call."""
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    call = SimpleNamespace(type="function", id="call_1", function=SimpleNamespace(name=name, arguments=args))
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                           usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))


def text_response(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class FakeCompletions:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("the fake model was called more times than scripted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeRawClient:
    """Stands in for the Meta API client (llm/meta_api.py): client.chat.completions.create(...)."""

    def __init__(self, responses: list):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


TEST_PASSWORD = "correct horse battery"


def sign_in(client, database_url: str, email: str = "admin@test.local", role: str = "admin") -> dict:
    """Signs a TestClient in (cookie) as a user with this role, creating the account if needed."""
    import asyncio

    import asyncpg

    from backend.app.access import hash_password

    async def make():
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                """INSERT INTO users (user_id, email, password_hash, role) VALUES ($1, $2, $3, $4)
                   ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role""",
                f"usr_{role}_{abs(hash(email)) % 10**8}", email, hash_password(TEST_PASSWORD), role)
        finally:
            await conn.close()

    asyncio.run(make())
    response = client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()
