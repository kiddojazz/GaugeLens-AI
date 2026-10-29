"""Our Meta Model API client against a fake HTTP server (httpx.MockTransport): request shapes, response objects,
retries on transient errors, and errors that must not be retried."""

import asyncio
import json

import httpx
import pytest

from backend.app.llm.meta_api import MetaAPI, MetaAPIError


def api(handler, retries=2):
    transport = httpx.MockTransport(handler)
    client = MetaAPI("https://api.example/v1/", "key", max_retries=retries,
                     http=httpx.AsyncClient(transport=transport, headers={"Authorization": "Bearer key"}))
    return client


def test_chat_completion_sends_the_payload_and_returns_attribute_objects():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "record", "arguments": "{\"x\": 1}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    result = asyncio.run(api(handler).chat.completions.create(model="muse-spark-1.3", messages=[{"role": "user", "content": "hi"}],
                                                             tools=None, tool_choice="auto"))
    assert seen["url"] == "https://api.example/v1/chat/completions" and seen["auth"] == "Bearer key"
    assert seen["body"] == {"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "hi"}], "tool_choice": "auto"}
    call = result.choices[0].message.tool_calls[0]
    assert (call.id, call.function.name, call.function.arguments) == ("c1", "record", '{"x": 1}')
    assert result.usage.prompt_tokens == 10 and result.choices[0].message.content is None


def test_rate_limits_and_server_errors_are_retried_then_succeed(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda s: _instant())
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow down"}})
        if len(calls) == 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    result = asyncio.run(api(handler).chat.completions.create(model="m", messages=[]))
    assert result.choices[0].message.content == "OK" and len(calls) == 3


async def _instant():
    return None


def test_client_errors_are_not_retried_and_carry_the_api_message():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "tool_choice must be auto"}})

    with pytest.raises(MetaAPIError) as err:
        asyncio.run(api(handler).chat.completions.create(model="m", messages=[]))
    assert err.value.status == 400 and "tool_choice must be auto" in str(err.value) and len(calls) == 1


def test_retries_give_up_after_the_limit(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda s: _instant())
    with pytest.raises(MetaAPIError) as err:
        asyncio.run(api(lambda r: httpx.Response(500, text="down"), retries=1).chat.completions.create(model="m", messages=[]))
    assert err.value.status == 500


def test_responses_api_text_and_file_upload():
    def handler(request):
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "<1f>0<|box;1;2;3;4|>"}, {"type": "output_text", "text": " done"}]}]})
        assert request.url.path.endswith("/files") and b'name="purpose"' in request.content and b"clip.mp4" in request.content
        return httpx.Response(200, json={"id": "file_123"})

    client = api(handler)
    response = asyncio.run(client.responses.create(model="sam-3.1", input=[], metadata={"a": "b"}))
    assert response.output_text == "<1f>0<|box;1;2;3;4|> done"
    uploaded = asyncio.run(client.files.create(file=("clip.mp4", b"\x00video"), purpose="user_data"))
    assert uploaded.id == "file_123"
