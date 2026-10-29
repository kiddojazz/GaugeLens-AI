"""The chat client for the Meta Model API (Muse Spark), over our own HTTP client (meta_api.py).

Structured output, chosen by LLM_STRUCTURED_MODE:
    json_schema -> response_format json_schema; the server constrains output to the schema. The default:
                   Meta's Chat Completions accepts only tool_choice="auto" (a tool call could be skipped).
    tool        -> ONE tool call whose parameters are the Pydantic schema (as GridIQ's
                   with_structured_output(method="function_calling")).
Either way the result is validated with Pydantic and retried once with the validation error.
Every call is cached (see cache.py) and described by a CallMeta for the model_calls table.
"""

import base64
import copy
import json
import logging
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..ids import new_id
from .cache import ResponseCache
from .meta_api import MetaAPI

log = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    """The model API could not be reached or returned an error (after transport retries)."""


class StructuredOutputError(RuntimeError):
    """The model's output never matched the schema, even after the retry with feedback."""

    def __init__(self, message: str, meta: "CallMeta | None" = None):
        super().__init__(message)
        self.meta = meta


class CacheMiss(RuntimeError):
    """CACHE_ONLY=true and this exact request was never cached."""


@dataclass
class CallMeta:
    call_id: str
    provider: str
    model: str
    purpose: str
    prompt_version: str
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit: bool = False
    ok: bool = True
    error: str | None = None
    attempts: int = 0
    extra: dict = field(default_factory=dict)


PROVIDER = "meta"


def make_raw_client(settings: Settings, timeout: float | None = None) -> MetaAPI:
    if not settings.meta_api_key:
        raise ModelUnavailable("MODEL_API_KEY must be set (Meta Model API).")
    return MetaAPI(settings.meta_base_url, settings.meta_api_key, timeout=timeout or settings.request_timeout_s,
                   max_retries=settings.max_retries)


def inline_refs(schema: dict) -> dict:
    """Pydantic puts nested models in $defs and points at them with $ref; some tool-calling APIs
    don't resolve refs, so nested definitions are inlined."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(copy.deepcopy(defs[ref.split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def image_part(png_bytes: bytes, detail: str | None = None, mime: str = "image/png") -> dict:
    """A chat image content part (data URI)."""
    url = {"url": f"data:{mime};base64,{base64.b64encode(png_bytes).decode('ascii')}"}
    if detail:
        url["detail"] = detail
    return {"type": "image_url", "image_url": url}


def _tool_arguments(response, tool_name: str) -> str | None:
    """The JSON arguments of the requested tool call; falls back to JSON in the message text."""
    message = response.choices[0].message
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if function is not None and function.name == tool_name:
            return function.arguments
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip().startswith("{"):
        return content.strip()
    return None


class ChatClient:
    """Muse Spark chat client. `raw_client` and `cache` are injectable for tests."""

    def __init__(self, settings: Settings, raw_client=None, cache: ResponseCache | None = None):
        self.settings = settings
        self._raw = raw_client
        self.cache = cache or ResponseCache(settings.cache_dir)

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def raw(self):
        if self._raw is None:
            self._raw = make_raw_client(self.settings)
        return self._raw

    def image(self, png_bytes: bytes) -> dict:
        return image_part(png_bytes, detail=self.settings.image_detail)

    def _tool_choice(self, tool_name: str):
        mode = self.settings.tool_choice
        if mode == "forced":
            return {"type": "function", "function": {"name": tool_name}}
        return mode                                   # "required" | "auto"

    async def _create(self, model: str, messages: list, **kwargs):
        if self.settings.temperature is not None:
            kwargs["temperature"] = self.settings.temperature
        if self.settings.reasoning_effort:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        try:
            return await self.raw.chat.completions.create(model=model, messages=messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - transport and API errors; callers get one type
            raise ModelUnavailable(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

    async def ping(self, model: str | None = None) -> tuple[str, int]:
        """Spike A: the smallest possible call. Returns (reply text, latency ms). Never cached."""
        start = time.perf_counter()
        response = await self._create(model or self.settings.read_model,
                                      [{"role": "user", "content": "Reply with the single word OK."}])
        return (response.choices[0].message.content or "").strip(), int((time.perf_counter() - start) * 1000)

    async def chat(self, messages: list, *, tools: list | None, purpose: str, prompt_version: str,
                   model: str | None = None) -> tuple[dict, CallMeta]:
        """One agent step: returns {"content": str | None, "tool_calls": [{"id", "name", "arguments"}]}.
        tool_choice is always "auto" (the only value Meta's Chat Completions accepts). Cached like structured()."""
        model = model or self.settings.read_model
        meta = CallMeta(call_id=new_id("call_"), provider=self.provider, model=model, purpose=purpose,
                        prompt_version=prompt_version)
        key = ResponseCache.key(provider=self.provider, model=model, purpose=purpose, prompt_version=prompt_version,
                                tools=tools, messages=messages, reasoning=self.settings.reasoning_effort)
        cached = self.cache.get(key)
        if cached is not None:
            meta.cache_hit = True
            meta.input_tokens, meta.output_tokens = cached.get("input_tokens"), cached.get("output_tokens")
            return cached["message"], meta
        if self.settings.cache_only:
            raise CacheMiss(f"CACHE_ONLY is set and there is no cached response for {purpose} ({key[:12]}).")
        start = time.perf_counter()
        kwargs = {"tools": tools, "tool_choice": "auto"} if tools else {}
        response = await self._create(model, messages, **kwargs)
        meta.latency_ms = int((time.perf_counter() - start) * 1000)
        usage = getattr(response, "usage", None)
        if usage is not None:
            meta.input_tokens = getattr(usage, "prompt_tokens", None)
            meta.output_tokens = getattr(usage, "completion_tokens", None)
        message = response.choices[0].message
        out = {"content": getattr(message, "content", None),
               "tool_calls": [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                              for c in (getattr(message, "tool_calls", None) or [])]}
        self.cache.put(key, {"message": out, "purpose": purpose, "model": model, "provider": self.provider,
                             "input_tokens": meta.input_tokens, "output_tokens": meta.output_tokens,
                             "latency_ms": meta.latency_ms})
        return out, meta

    async def structured(self, schema: type[BaseModel], messages: list, *, purpose: str, prompt_version: str,
                         tool_name: str, tool_description: str, model: str | None = None) -> tuple[BaseModel, CallMeta]:
        """Asks for exactly one tool call matching `schema`, validates it, retries once with the
        validation error, and caches the validated result."""
        model = model or self.settings.read_model
        mode = self.settings.structured_mode
        parameters = inline_refs(schema.model_json_schema())
        meta = CallMeta(call_id=new_id("call_"), provider=self.provider, model=model, purpose=purpose,
                        prompt_version=prompt_version)
        key = ResponseCache.key(provider=self.provider, model=model, purpose=purpose, prompt_version=prompt_version,
                                tool=tool_name, schema=parameters, messages=messages, mode=mode,
                                reasoning=self.settings.reasoning_effort)

        cached = self.cache.get(key)
        if cached is not None:
            meta.cache_hit = True
            meta.input_tokens, meta.output_tokens = cached.get("input_tokens"), cached.get("output_tokens")
            return schema.model_validate_json(cached["arguments"]), meta
        if self.settings.cache_only:
            raise CacheMiss(f"CACHE_ONLY is set and there is no cached response for {purpose} ({key[:12]}).")

        if mode == "json_schema":
            request = {"response_format": {"type": "json_schema",
                                           "json_schema": {"name": tool_name, "schema": parameters}}}
        else:
            request = {"tools": [{"type": "function", "function": {"name": tool_name, "description": tool_description,
                                                                   "parameters": parameters}}],
                       "tool_choice": self._tool_choice(tool_name)}
        conversation = list(messages)
        start = time.perf_counter()
        last_error = "no structured output returned"
        for attempt in range(1, self.settings.max_schema_retries + 2):
            meta.attempts = attempt
            response = await self._create(model, conversation, **request)
            usage = getattr(response, "usage", None)
            if usage is not None:
                meta.input_tokens = (meta.input_tokens or 0) + (getattr(usage, "prompt_tokens", 0) or 0)
                meta.output_tokens = (meta.output_tokens or 0) + (getattr(usage, "completion_tokens", 0) or 0)
            arguments = _tool_arguments(response, tool_name)
            if arguments is not None:
                try:
                    result = schema.model_validate_json(arguments)
                except ValidationError as exc:
                    last_error = str(exc)[:1500]
                else:
                    meta.latency_ms = int((time.perf_counter() - start) * 1000)
                    self.cache.put(key, {"arguments": arguments, "purpose": purpose, "model": model,
                                         "provider": self.provider, "prompt_version": prompt_version,
                                         "input_tokens": meta.input_tokens, "output_tokens": meta.output_tokens,
                                         "latency_ms": meta.latency_ms})
                    return result, meta
            log.info("%s: output did not match the schema (attempt %d): %s", purpose, attempt, last_error[:200])
            retry = (f"Return the `{tool_name}` JSON again, matching its schema exactly." if mode == "json_schema"
                     else f"Call `{tool_name}` again with arguments that match its schema exactly.")
            conversation = [*conversation,
                            {"role": "assistant", "content": arguments or "(no structured output)"},
                            {"role": "user", "content": f"That output was not valid for `{tool_name}`: {last_error}\n{retry}"}]

        meta.latency_ms = int((time.perf_counter() - start) * 1000)
        meta.ok, meta.error = False, last_error
        raise StructuredOutputError(f"{purpose}: no valid `{tool_name}` call after {meta.attempts} attempts: "
                                    f"{last_error[:300]}", meta=meta)
