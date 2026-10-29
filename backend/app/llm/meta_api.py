"""A small async client for the Meta Model API (Muse Spark chat, SAM 3.1 segmentation, file uploads).

Plain HTTPS with httpx, shaped like the calls the rest of the code makes:
    raw.chat.completions.create(model=..., messages=..., tools=..., response_format=...)  -> .choices[0].message
    raw.responses.create(model=..., input=..., metadata=...)                             -> .output_text
    raw.files.create(file=(name, bytes), purpose="user_data")                            -> .id
Responses come back as attribute objects (response.choices[0].message.tool_calls[0].function.arguments).
Transient failures (429, 5xx, timeouts, dropped connections) are retried with exponential backoff,
honouring Retry-After. Test doubles only need the same attribute shape.
"""

import asyncio
import logging
from types import SimpleNamespace

import httpx

log = logging.getLogger(__name__)
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class MetaAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def as_object(value):
    """JSON -> nested attribute objects (lists stay lists)."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: as_object(v) for k, v in value.items()})
    if isinstance(value, list):
        return [as_object(v) for v in value]
    return value


def _output_text(body: dict) -> str:
    """The concatenated text of a Responses API reply (its output[].content[] text parts)."""
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    parts = [c.get("text", "") for item in body.get("output") or [] if item.get("type") == "message"
             for c in item.get("content") or [] if c.get("type") in ("output_text", "text")]
    return "".join(parts)


class MetaAPI:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 90.0, max_retries: int = 2,
                 http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.http = http or httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat_completion))
        self.responses = SimpleNamespace(create=self._response)
        self.files = SimpleNamespace(create=self._upload)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.http.request(method, f"{self.base_url}{path}", **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    raise MetaAPIError(0, f"{type(exc).__name__}: {exc}") from exc
            else:
                if response.status_code < 400:
                    return response.json()
                if response.status_code not in RETRY_STATUS or attempt == self.max_retries:
                    try:
                        detail = response.json().get("error", {})
                        message = detail.get("message") if isinstance(detail, dict) else str(detail)
                    except ValueError:
                        message = response.text
                    raise MetaAPIError(response.status_code, (message or response.reason_phrase)[:500])
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    delay = max(delay, float(retry_after))
            log.info("Meta API %s %s: retrying in %.1f s (attempt %d)", method, path, delay, attempt + 1)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)
        raise MetaAPIError(0, "retries exhausted")                    # not reached

    async def _chat_completion(self, **payload):
        body = {k: v for k, v in payload.items() if v is not None}
        return as_object(await self._request("POST", "/chat/completions", json=body))

    async def _response(self, **payload):
        body = await self._request("POST", "/responses", json={k: v for k, v in payload.items() if v is not None})
        result = as_object(body)
        result.output_text = _output_text(body)
        return result

    async def _upload(self, *, file: tuple[str, bytes], purpose: str):
        name, data = file
        return as_object(await self._request("POST", "/files", data={"purpose": purpose},
                                             files={"file": (name, data, "application/octet-stream")}))
