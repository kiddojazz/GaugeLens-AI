"""Frame sampling and change detection.

Frames are sampled at a fixed rate (SAMPLE_FPS, default 2), lowered for long videos so a clip never has more than
MAX_FRAMES (default 60): every frame is a model call, and a 50 s clip at 2 fps took minutes. A display is only
re-read when its crop changed or REREAD_INTERVAL_S passed; LCD values change slowly, so this is the main
model-credit saver.

Photos (photo_frames) are a clip of still frames, one per photo, in the order sent.

"Changed" = the LARGEST mean grey-level difference over 16x16-pixel blocks of a fixed 160x48 thumbnail,
above CHANGE_THRESHOLD (default 10). A whole-crop mean missed a page change that only moved a few digits
("230 V" -> "23.6 V", mean 2.6); per block, unchanged mock frames score <= 1.8 and real changes >= 18.8.
Handheld phone footage needs the display TRACKED (SAM 3.1 video, discovery.py): with a fixed box a static
display scored 10-61; with tracked boxes it scores 2-9.
"""

import io
from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageOps


@dataclass
class SampledFrame:
    index: int                 # sample number (0, 1, 2, ...)
    t_offset_s: float          # seconds from the start of the video
    jpeg: bytes
    size: tuple[int, int]      # (width, height)


def effective_fps(video_path: str, fps: float, max_frames: int | None) -> float:
    """The sampling rate for this video: `fps`, lowered so a long video yields at most `max_frames` frames."""
    if not max_frames:
        return fps
    cap = cv2.VideoCapture(str(video_path))
    try:
        source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / source_fps
    finally:
        cap.release()
    return min(fps, max_frames / duration) if duration * fps > max_frames else fps


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


PHOTO_GAP_S = 300.0            # photos without capture times: spaced so no rule combines values from two photos
MAX_PHOTO_SIDE = 2560
MAX_PHOTO_PIXELS = 120_000_000     # checked before decoding: a tiny file can unpack to gigabytes (the largest phones: 200 MP)
_EXIF_TIMES = (36867, 36868, 306)   # DateTimeOriginal, DateTimeDigitized, DateTime


def _taken_at(img: Image.Image) -> datetime | None:
    exif = img.getexif()
    sub = exif.get_ifd(0x8769) if exif else {}
    for tag in _EXIF_TIMES:
        raw = sub.get(tag) or exif.get(tag)
        if raw:
            try:
                return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
    return None


def normalise_photo(data: bytes, jpeg_quality: int = 92) -> tuple[bytes, datetime | None]:
    """(JPEG bytes, when it was taken). Upright (phones store rotation as a tag), at most MAX_PHOTO_SIDE pixels,
    and with all metadata dropped: phone photos carry the GPS location, which GaugeLens never needs to keep."""
    try:
        img = Image.open(io.BytesIO(data))           # reads only the header
        if img.width * img.height > MAX_PHOTO_PIXELS:
            raise ValueError(f"this photo is too large ({img.width} x {img.height} pixels); "
                             "use a lower camera resolution")
        img.draft("RGB", (MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))    # JPEG: decode at reduced size when it can
        img.load()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decoder error means "not a photo we can read"
        raise ValueError("this file isn't a photo GaugeLens can read (use JPEG, PNG or WebP)") from exc
    taken = _taken_at(img)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=jpeg_quality)          # no exif= argument: nothing of the original's metadata
    return out.getvalue(), taken


def photo_times(taken: list[datetime | None]) -> tuple[list[float], bool]:
    """Seconds from the first photo for each photo, and whether those are real capture times. Without a capture
    time on every photo, photos are PHOTO_GAP_S apart: rules that need a real time between readings don't run."""
    if taken and all(taken) and len(set(taken)) == len(taken):
        first = min(taken)
        return [(t - first).total_seconds() for t in taken], True
    return [i * PHOTO_GAP_S for i in range(len(taken))], False


def photo_frames(photos: list[tuple[bytes, float]]) -> list[SampledFrame]:
    """One frame per (normalised JPEG, t_offset_s), in time order."""
    frames = []
    for jpeg, t in sorted(photos, key=lambda p: p[1]):
        size = Image.open(io.BytesIO(jpeg)).size
        frames.append(SampledFrame(len(frames), round(t, 3), jpeg, size))
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
