import asyncio
import json

import pytest
from pydantic import BaseModel

from backend.app.llm import CacheMiss, ChatClient, ModelUnavailable, StructuredOutputError
from backend.app.llm.client import inline_refs
from backend.tests.fakes import FakeRawClient, make_settings, text_response, tool_response


class Inner(BaseModel):
    x: int


class Outer(BaseModel):
    name: str
    items: list[Inner]


def ask(client: ChatClient, messages=None):
    return asyncio.run(client.structured(Outer, messages or [{"role": "user", "content": "hi"}], purpose="test",
                                         prompt_version="v1", tool_name="record", tool_description="d"))


def test_valid_tool_call_is_parsed_and_described(tmp_path):
    raw = FakeRawClient([tool_response({"name": "a", "items": [{"x": 1}]}, name="record")])
    result, meta = ask(ChatClient(make_settings(tmp_path), raw_client=raw))
    assert result.items[0].x == 1
    assert meta.ok and not meta.cache_hit and meta.attempts == 1 and meta.input_tokens == 1200
    call = raw.completions.calls[0]
    assert call["model"] == "muse-spark-1.3"
    assert call["tool_choice"] == {"type": "function", "function": {"name": "record"}}
    assert "temperature" not in call                    # not configured -> the model's default


def test_second_identical_call_comes_from_the_cache(tmp_path):
    settings = make_settings(tmp_path)
    raw = FakeRawClient([tool_response({"name": "a", "items": []}, name="record")])
    ask(ChatClient(settings, raw_client=raw))
    result, meta = ask(ChatClient(settings, raw_client=raw))          # would fail if the model were called again
    assert meta.cache_hit and result.name == "a" and len(raw.completions.calls) == 1


def test_invalid_output_is_retried_with_the_validation_error(tmp_path):
    raw = FakeRawClient([tool_response({"name": "a", "items": [{"x": "not a number"}]}, name="record"),
                         tool_response({"name": "a", "items": [{"x": 2}]}, name="record")])
    result, meta = ask(ChatClient(make_settings(tmp_path), raw_client=raw))
    assert result.items[0].x == 2 and meta.attempts == 2
    feedback = raw.completions.calls[1]["messages"][-1]["content"]
    assert "not valid" in feedback and "record" in feedback


def test_still_invalid_after_retry_raises_with_meta(tmp_path):
    raw = FakeRawClient([text_response("I think it says 24 volts"), text_response("still prose")])
    with pytest.raises(StructuredOutputError) as err:
        ask(ChatClient(make_settings(tmp_path), raw_client=raw))
    assert err.value.meta is not None and err.value.meta.ok is False and err.value.meta.attempts == 2


def test_json_in_message_text_is_accepted_when_no_tool_call(tmp_path):
    raw = FakeRawClient([text_response('{"name": "b", "items": []}')])
    result, _ = ask(ChatClient(make_settings(tmp_path), raw_client=raw))
    assert result.name == "b"


def test_cache_only_never_calls_the_model(tmp_path):
    raw = FakeRawClient([])
    with pytest.raises(CacheMiss):
        ask(ChatClient(make_settings(tmp_path, cache_only=True), raw_client=raw))
    assert raw.completions.calls == []


def test_provider_errors_become_model_unavailable(tmp_path):
    raw = FakeRawClient([TimeoutError("read timed out")])
    with pytest.raises(ModelUnavailable):
        ask(ChatClient(make_settings(tmp_path), raw_client=raw))


def test_temperature_is_sent_when_configured(tmp_path):
    raw = FakeRawClient([tool_response({"name": "a", "items": []}, name="record")])
    ask(ChatClient(make_settings(tmp_path, temperature=0.0), raw_client=raw))
    assert raw.completions.calls[0]["temperature"] == 0.0


def test_reasoning_effort_is_sent_only_when_configured(tmp_path):
    raw = FakeRawClient([tool_response({"name": "a", "items": []}, name="record"),
                         tool_response({"name": "b", "items": []}, name="record")])
    ask(ChatClient(make_settings(tmp_path), raw_client=raw))
    ask(ChatClient(make_settings(tmp_path, reasoning_effort="none"), raw_client=raw),
        messages=[{"role": "user", "content": "other"}])
    assert "reasoning_effort" not in raw.completions.calls[0]
    assert raw.completions.calls[1]["reasoning_effort"] == "none"


def test_json_schema_mode_uses_response_format_and_parses_content(tmp_path):
    raw = FakeRawClient([text_response('{"name": "m", "items": [{"x": 3}]}')])
    settings = make_settings(tmp_path, meta_api_key="k", structured_mode="json_schema",
                             tool_choice="auto")
    result, meta = ask(ChatClient(settings, raw_client=raw))
    call = raw.completions.calls[0]
    assert result.items[0].x == 3 and meta.model == "muse-spark-1.3"
    assert call["response_format"]["type"] == "json_schema" and call["response_format"]["json_schema"]["name"] == "record"
    assert "$defs" not in json.dumps(call["response_format"]) and "tools" not in call and "tool_choice" not in call


def test_required_tool_choice_mode(tmp_path):
    raw = FakeRawClient([tool_response({"name": "a", "items": []}, name="record")])
    ask(ChatClient(make_settings(tmp_path, tool_choice="required"), raw_client=raw))
    assert raw.completions.calls[0]["tool_choice"] == "required"


def test_nested_models_are_inlined_for_tool_schemas():
    schema = inline_refs(Outer.model_json_schema())
    assert "$defs" not in schema and "$ref" not in str(schema)
    assert schema["properties"]["items"]["items"]["properties"]["x"]["type"] == "integer"


def test_ping_returns_text_and_latency(tmp_path):
    raw = FakeRawClient([text_response("OK")])
    reply, latency = asyncio.run(ChatClient(make_settings(tmp_path), raw_client=raw).ping())
    assert reply == "OK" and latency >= 0
