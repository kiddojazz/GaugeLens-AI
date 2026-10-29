"""SAM 3.1 on Meta Model API (Responses API, model "sam-3.1"): concept segmentation of images and video.

Output is special-token text, one line per frame (docs: dev.meta.ai/docs/sam/reading-segmentation):
    <0f>0<|box;x1=244;y1=238;x2=884;y2=492;w=1400;h=820;c=0.81|><|mask;x=0;y=0;c=0.81;data=255,641,!...|>,1<|box;...|><|mask;...|>
  - one record per match: object id, box (absolute source pixels, xyxy), mask (box-local, base85 payload)
  - video: object ids are stable across frames; frame indices may skip and arrive out of order
  - zero matches = empty text (a valid result)
We use boxes (crops, ROIs, re-look). Masks are kept as raw tokens; nothing in v1 needs them decoded.
Every call is cached by hash(model, prompt, media bytes) like the chat client.
"""

import base64
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..ids import new_id
from .cache import ResponseCache
from .client import CacheMiss, CallMeta, ModelUnavailable

SAM_MODEL = "sam-3.1"
_FRAME = re.compile(r"^<(\d+)f>(.*)$")
_RECORD = re.compile(r"(\d+)<\|box;(.*?)\|><\|mask;(.*?)\|>(?=,\d+<\|box|\s*$)", re.DOTALL)


@dataclass
class Detection:
    object_id: int
    frame: int
    box: tuple[float, float, float, float]          # x1, y1, x2, y2 in source pixels
    frame_size: tuple[int, int]                     # (w, h) of the source frame
    confidence: float | None = None
    mask_token: str = field(default="", repr=False)

    @property
    def xywh(self) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = self.box
        return round(x1), round(y1), round(x2 - x1), round(y2 - y1)


def _fields(token: str) -> dict:
    out = {}
    for part in token.split(";"):
        key, sep, value = part.partition("=")
        if sep and key != "data":
            out[key.strip()] = value.strip()
    return out


def parse_sam_output(text: str) -> list[Detection]:
    """Parses the SAM token text into detections (all frames). Empty text -> []."""
    detections = []
    for line in (text or "").splitlines():
        m = _FRAME.match(line.strip())
        if not m:
            continue
        frame = int(m.group(1))
        for rec in _RECORD.finditer(m.group(2)):
            box = _fields(rec.group(2))
            mask = _fields(rec.group(3))
            confidence = box.get("c") or mask.get("c")
            detections.append(Detection(
                object_id=int(rec.group(1)), frame=frame,
                box=(float(box["x1"]), float(box["y1"]), float(box["x2"]), float(box["y2"])),
                frame_size=(int(box["w"]), int(box["h"])),
                confidence=float(confidence) if confidence is not None else None,
                mask_token=rec.group(3)))
    return detections


def iou(a: tuple, b: tuple) -> float:
    """IoU of two xyxy boxes."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class SamClient:
    """`raw_client` (a MetaAPI, see meta_api.py) and `cache` are injectable for tests."""

    def __init__(self, settings: Settings, raw_client=None, cache: ResponseCache | None = None):
        self.settings = settings
        self._raw = raw_client
        self.cache = cache or ResponseCache(settings.cache_dir)

    @property
    def raw(self):
        if self._raw is None:
            from .client import make_raw_client
            self._raw = make_raw_client(self.settings, timeout=max(self.settings.request_timeout_s, 300))
        return self._raw

    async def _segment(self, media_part: dict, media_key: str, prompt: str, purpose: str) -> tuple[list[Detection], CallMeta]:
        meta = CallMeta(call_id=new_id("call_"), provider="meta", model=SAM_MODEL, purpose=purpose, prompt_version="sam_v1")
        key = ResponseCache.key(provider="meta", model=SAM_MODEL, purpose=purpose, prompt=prompt, media=media_key)
        cached = self.cache.get(key)
        if cached is not None:
            meta.cache_hit = True
            return parse_sam_output(cached["text"]), meta
        if self.settings.cache_only:
            raise CacheMiss(f"CACHE_ONLY is set and SAM output for {prompt!r} is not cached.")
        start = time.perf_counter()
        try:
            response = await self.raw.responses.create(
                model=SAM_MODEL,
                input=[{"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": prompt}, media_part]}],
                metadata={"include_confidence": "true", "mask_encoding": "one_bit"})
        except Exception as exc:  # noqa: BLE001 - one error type for callers
            raise ModelUnavailable(f"SAM 3.1: {type(exc).__name__}: {str(exc)[:300]}") from exc
        meta.latency_ms = int((time.perf_counter() - start) * 1000)
        text = response.output_text or ""
        self.cache.put(key, {"text": text, "purpose": purpose, "model": SAM_MODEL, "prompt": prompt,
                             "latency_ms": meta.latency_ms})
        return parse_sam_output(text), meta

    async def segment_image(self, image_bytes: bytes, prompt: str, mime: str = "image/png") -> tuple[list[Detection], CallMeta]:
        """All instances of a noun phrase ('digital display', 'nameplate') in one image."""
        data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        return await self._segment({"type": "input_image", "image_url": data_url},
                                   ResponseCache.key(image=data_url), prompt, "sam_image")

    async def track_video(self, video_path: Path, prompt: str) -> tuple[list[Detection], CallMeta]:
        """Uploads the clip (Files API) and tracks every instance; object ids are stable across frames."""
        data = Path(video_path).read_bytes()
        media_key = ResponseCache.key(video=base64.b64encode(data[:1_000_000]).decode("ascii"), size=len(data))
        cached = self.cache.get(ResponseCache.key(provider="meta", model=SAM_MODEL, purpose="sam_video",
                                                  prompt=prompt, media=media_key))
        if cached is None and not self.settings.cache_only:
            try:
                uploaded = await self.raw.files.create(file=(Path(video_path).name, data), purpose="user_data")
            except Exception as exc:  # noqa: BLE001
                raise ModelUnavailable(f"Files API upload: {type(exc).__name__}: {str(exc)[:300]}") from exc
            part = {"type": "input_video", "file_id": uploaded.id}
        else:
            part = {"type": "input_video", "file_id": "cached"}
        return await self._segment(part, media_key, prompt, "sam_video")
