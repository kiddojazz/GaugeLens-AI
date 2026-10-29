"""Spike C: SAM 3.1 for discovery, narrow-prompt re-look, nameplates and video tracking.

    python -m eval.spikes.sam_spike discovery     # mock panel screenshots vs hand-drawn ROIs (IoU)
    python -m eval.spikes.sam_spike narrow        # narrow prompts on one inverter screenshot
    python -m eval.spikes.sam_spike photos        # real nameplate photos in data/photos/substation
    python -m eval.spikes.sam_spike video         # tracking on data/mock_clips/mock_day1.mp4
Annotated images: eval/spikes/screens/sam/.
"""

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.llm.sam import SamClient, iou
from backend.app.seed import MOCK

OUT = PROJECT_ROOT / "eval" / "spikes" / "screens" / "sam"
SHOTS = PROJECT_ROOT / "eval" / "spikes" / "screens" / "mock_spike"
ROIS = {d[0]: (d[3]["x"], d[3]["y"], d[3]["x"] + d[3]["w"], d[3]["y"] + d[3]["h"]) for d in MOCK["displays"]}
COLORS = ["#ff3b30", "#34c759", "#0a84ff", "#ff9f0a", "#bf5af2", "#64d2ff"]


def annotate(image_path: Path, detections, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    for n, d in enumerate(detections):
        color = COLORS[n % len(COLORS)]
        draw.rectangle(d.box, outline=color, width=4)
        draw.text((d.box[0] + 4, d.box[1] + 4), f"{d.object_id} c={d.confidence:.2f}" if d.confidence else str(d.object_id), fill=color)
    path = OUT / name
    image.save(path)
    return path


async def discovery(sam: SamClient) -> None:
    for shot in sorted(SHOTS.glob("seed1_p*_*.png")):
        dets, meta = await sam.segment_image(shot.read_bytes(), "digital display")
        matches = {roi: max((iou(d.box, box) for d in dets), default=0) for roi, box in ROIS.items()}
        print(f"{shot.name:<22} {len(dets)} found ({meta.latency_ms} ms{' cached' if meta.cache_hit else ''})  "
              + "  ".join(f"{k}: IoU {v:.3f}" for k, v in matches.items()))
        annotate(shot, dets, f"discovery_{shot.name}")


async def narrow(sam: SamClient) -> None:
    shot = SHOTS / "seed1_p1_off.png"            # OUTPUT page: 338 W, 393 VA, 11 %
    for prompt in ["seven-segment digits", "digits", "number", "the largest number on the screen",
                   "watt reading", "clamp meter digits", "LCD screen", "unit label"]:
        dets, meta = await sam.segment_image(shot.read_bytes(), prompt)
        boxes = "  ".join(f"[{d.object_id}] {tuple(round(v) for v in d.box)} c={d.confidence:.2f}" for d in dets)
        print(f"{prompt!r:<36} {len(dets)} ({meta.latency_ms} ms): {boxes or '(none)'}")
        annotate(shot, dets, f"narrow_{prompt.replace(' ', '_')}.png")


async def photos(sam: SamClient) -> None:
    for photo in sorted((PROJECT_ROOT / "data" / "photos" / "substation").glob("*.*")):
        mime = "image/png" if photo.suffix.lower() == ".png" else "image/jpeg"
        for prompt in ["nameplate", "current transformer"]:
            dets, meta = await sam.segment_image(photo.read_bytes(), prompt, mime=mime)
            print(f"{photo.name:<38} {prompt!r:<22} {len(dets)} ({meta.latency_ms} ms) "
                  + " ".join(f"c={d.confidence:.2f}" for d in dets if d.confidence))
            annotate(photo, dets, f"photo_{prompt.replace(' ', '_')}_{photo.stem}.png")


async def video(sam: SamClient) -> None:
    clip = PROJECT_ROOT / "data" / "mock_clips" / "mock_day1.mp4"
    dets, meta = await sam.track_video(clip, "digital display")
    frames = sorted({d.frame for d in dets})
    by_object = defaultdict(list)
    for d in dets:
        by_object[d.object_id].append(d)
    print(f"{clip.name}: {len(dets)} detections over {len(frames)} frames, {len(by_object)} object ids "
          f"({meta.latency_ms} ms{' cached' if meta.cache_hit else ''})")
    for object_id, ds in sorted(by_object.items()):
        best_roi = max(ROIS, key=lambda r: sum(iou(d.box, ROIS[r]) for d in ds))
        ious = [iou(d.box, ROIS[best_roi]) for d in ds]
        print(f"  object {object_id}: {len(ds)} frames -> {best_roi}, IoU min {min(ious):.3f} mean {sum(ious) / len(ious):.3f}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "discovery"
    sam = SamClient(get_settings())
    asyncio.run({"discovery": discovery, "narrow": narrow, "photos": photos, "video": video}[mode](sam))
    return 0


if __name__ == "__main__":
    sys.exit(main())
