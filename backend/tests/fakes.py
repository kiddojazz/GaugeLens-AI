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
