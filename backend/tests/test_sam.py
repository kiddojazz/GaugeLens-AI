import asyncio
from types import SimpleNamespace

import pytest

from backend.app.llm import CacheMiss
from backend.app.llm.sam import SamClient, iou, parse_sam_output
from backend.tests.fakes import make_settings

# Real SAM 3.1 output for the mock panel, prompt "digital display" (Oct 26), mask payloads shortened.
REAL = ("<0f>0<|box;x1=244;y1=238;x2=884;y2=492;w=1400;h=820;c=0.8128654956817627|>"
        "<|mask;x=0;y=0;c=0.8128654956817627;data=255,641,!!!!#&H3k*@0'Q^W.aL'4#SVg4|>,"
        "1<|box;x1=973;y1=301;x2=1161;y2=411;w=1400;h=820;c=0.7344398498535156|>"
        "<|mask;x=0;y=0;c=0.7344398498535156;data=111,189,!!!!!UHn'CaIU]-_!/Uu|>")


def test_real_output_parses_to_boxes_with_confidence():
    inverter, clamp = parse_sam_output(REAL)
    assert inverter.object_id == 0 and inverter.frame == 0 and inverter.box == (244, 238, 884, 492)
    assert inverter.frame_size == (1400, 820) and round(inverter.confidence, 2) == 0.81
    assert clamp.xywh == (973, 301, 188, 110)
    assert iou(inverter.box, (245, 238, 885, 493)) > 0.99            # matches the hand-drawn ROI


def test_video_lines_keep_object_ids_and_empty_output_is_valid():
    text = ("<0f>3<|box;x1=1;y1=2;x2=11;y2=12;w=100;h=50|><|mask;x=0;y=0;data=10,10,!ab|>\n"
            "<2f>3<|box;x1=2;y1=2;x2=12;y2=12;w=100;h=50|><|mask;x=0;y=0;data=10,10,!ab|>,"
            "7<|box;x1=50;y1=5;x2=60;y2=15;w=100;h=50|><|mask;x=0;y=0;data=10,10,!cd|>\n")
    dets = parse_sam_output(text)
    assert [(d.frame, d.object_id) for d in dets] == [(0, 3), (2, 3), (2, 7)]
    assert dets[0].confidence is None
    assert parse_sam_output("") == []


class FakeResponses:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.text)


def test_segment_image_request_shape_and_cache(tmp_path):
    responses = FakeResponses(REAL)
    raw = SimpleNamespace(responses=responses)
    client = SamClient(make_settings(tmp_path, meta_api_key="k"), raw_client=raw)
    dets, meta = asyncio.run(client.segment_image(b"\x89PNG fake", "digital display"))
    assert len(dets) == 2 and not meta.cache_hit
    call = responses.calls[0]
    assert call["model"] == "sam-3.1" and call["metadata"]["include_confidence"] == "true"
    text_part, image_part = call["input"][0]["content"]
    assert text_part == {"type": "input_text", "text": "digital display"}
    assert image_part["image_url"].startswith("data:image/png;base64,")
    again, meta2 = asyncio.run(client.segment_image(b"\x89PNG fake", "digital display"))
    assert meta2.cache_hit and len(again) == 2 and len(responses.calls) == 1


def test_cache_only_never_calls_sam(tmp_path):
    raw = SimpleNamespace(responses=FakeResponses(REAL))
    client = SamClient(make_settings(tmp_path, meta_api_key="k", cache_only=True), raw_client=raw)
    with pytest.raises(CacheMiss):
        asyncio.run(client.segment_image(b"x", "digital display"))
