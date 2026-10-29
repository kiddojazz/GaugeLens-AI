"""Frame sampling and change detection.

Frames are sampled at a fixed rate (SAMPLE_FPS, default 2). A display is only re-read when its crop
changed or REREAD_INTERVAL_S passed; LCD values change slowly, so this is the main model-credit saver.

"Changed" = the LARGEST mean grey-level difference over 16x16-pixel blocks of a fixed 160x48 thumbnail,
above CHANGE_THRESHOLD (default 10). A whole-crop mean missed a page change that only moved a few digits
("230 V" -> "23.6 V", mean 2.6); per block, unchanged mock frames score <= 1.8 and real changes >= 18.8.
Handheld phone footage needs the display TRACKED (SAM 3.1 video, discovery.py): with a fixed box a static
display scored 10-61; with tracked boxes it scores 2-9.
"""

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image


@dataclass
class SampledFrame:
    index: int                 # sample number (0, 1, 2, ...)
    t_offset_s: float          # seconds from the start of the video
    jpeg: bytes
    size: tuple[int, int]      # (width, height)


def sample_frames(video_path: str, fps: float = 2.0, jpeg_quality: int = 92) -> list[SampledFrame]:
    """Decodes the video and keeps one frame every 1/fps seconds (by timestamp, not frame count)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"cannot open video {video_path}")
    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames, frame_no, next_t, step = [], 0, 0.0, 1.0 / fps
    try:
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            t = frame_no / source_fps
            frame_no += 1
            if t + 1e-6 < next_t:
                continue
            ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            if ok:
                frames.append(SampledFrame(len(frames), round(t, 3), encoded.tobytes(), (bgr.shape[1], bgr.shape[0])))
            next_t += step
    finally:
        cap.release()
    return frames


def scale_roi(roi: dict, drawn_on: dict | None, frame_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """ROIs are stored in the pixel space they were drawn on; scale them to this clip's frame size."""
    sx = frame_size[0] / drawn_on["w"] if drawn_on else 1.0
    sy = frame_size[1] / drawn_on["h"] if drawn_on else 1.0
    return (round(roi["x"] * sx), round(roi["y"] * sy), round(roi["w"] * sx), round(roi["h"] * sy))


def crop_signature(jpeg: bytes, box: tuple[int, int, int, int], size: tuple[int, int] = (160, 48)) -> np.ndarray:
    """A fixed-size greyscale thumbnail of the display crop, used only for change detection. Fixed size,
    so crops from a tracked (moving, zooming) box stay comparable frame to frame."""
    x, y, w, h = box
    image = Image.open(io.BytesIO(jpeg)).convert("L").crop((x, y, x + w, y + h))
    return np.asarray(image.resize(size, Image.BILINEAR), dtype=np.float32)


def change_score(previous: np.ndarray, current: np.ndarray, block: int = 16) -> float:
    """Largest per-block mean absolute difference: a few changed digits stand out against a still LCD."""
    diff = np.abs(previous - current)
    h, w = diff.shape
    return max(float(diff[y:y + block, x:x + block].mean()) for y in range(0, h, block) for x in range(0, w, block))


def changed(previous: np.ndarray | None, current: np.ndarray, threshold: float) -> bool:
    if previous is None or previous.shape != current.shape:
        return True
    return change_score(previous, current) >= threshold
