"""Deterministic seven-segment decoder: an independent second reader for the re-look.

SAM 3.1 boxes each digit ("number" prompt); this code decides which of the 7 segments inside the box are
dark. No model is involved, so it can't be talked into a 9 by a faint ghost outline: the MIDDLE segment
decides 7 vs 9 in every font (a 7 never lights it; many fonts light a 7's upper-left segment).

        a
      f   b        each segment is a BAND of the digit box; its darkness is the darkest line across the
        g          band (column means for vertical segments, row means for horizontal ones). That works
      e   c        whether SAM boxed the whole digit cell (faint ghosts visible) or only the lit segments.
        d

It abstains (returns None) whenever a segment is ambiguous, the box is too small, or the pattern isn't
a digit. A thin box (width < 0.35 x height) is a "1" (SAM boxes a lit 1 narrowly). A cell with no lit
segment is blank (a leading blank digit position), not a digit.
"""

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

# segment -> (orientation, x0, x1, y0, y1) as fractions of the digit box
_BANDS = {"a": ("h", 0.30, 0.70, 0.00, 0.20), "g": ("h", 0.30, 0.70, 0.38, 0.62), "d": ("h", 0.30, 0.70, 0.80, 1.00),
          "f": ("v", 0.00, 0.40, 0.15, 0.40), "b": ("v", 0.60, 1.00, 0.15, 0.40),
          "e": ("v", 0.00, 0.40, 0.60, 0.85), "c": ("v", 0.60, 1.00, 0.60, 0.85)}
_PATTERNS = {
    "abcdef": "0", "bc": "1", "abdeg": "2", "abcdg": "3", "bcfg": "4", "acdfg": "5", "acdefg": "6",
    "abc": "7", "abcf": "7",                # some fonts light the upper-left segment of a 7
    "abcdefg": "8", "abcdfg": "9", "abcfg": "9",   # some fonts draw a 9 without its bottom segment
    "cdefg": "6",                            # 6 without its top segment
}
MIN_HEIGHT_PX = 16
AMBIGUOUS_BAND = 0.18                       # fraction of (background - lit) around the threshold


@dataclass
class DecodedDigit:
    digit: str | None
    segments: str
    ambiguous: str                           # segments too close to the threshold


def _gray(image_bytes: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(image_bytes)).convert("L"), dtype=np.float32)


def decode_digit(gray: np.ndarray, box: tuple[float, float, float, float]) -> DecodedDigit:
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    x1, y1 = max(0, x1), max(0, y1)
    cell = gray[y1:y2, x1:x2]
    h, w = cell.shape if cell.size else (0, 0)
    if h < MIN_HEIGHT_PX or w < 2:
        return DecodedDigit(None, "", "too small")
    if w < 0.35 * h:
        return DecodedDigit("1", "bc", "")
    lit, background = np.percentile(cell, 5), np.percentile(cell, 90)
    if background - lit < 25:                # nothing lit: a blank (ghost-only) cell
        return DecodedDigit("", "", "")
    threshold = (lit + background) / 2
    band = AMBIGUOUS_BAND * (background - lit)
    on, unsure = "", ""
    for name, (orientation, fx0, fx1, fy0, fy1) in _BANDS.items():
        region = cell[int(fy0 * h):max(int(fy0 * h) + 1, int(fy1 * h)), int(fx0 * w):max(int(fx0 * w) + 1, int(fx1 * w))]
        lines = region.mean(axis=0) if orientation == "v" else region.mean(axis=1)
        value = float(lines.min())
        if abs(value - threshold) < band:
            unsure += name
        elif value < threshold:
            on += name
    if not on and not unsure:
        return DecodedDigit("", "", "")
    digit = _PATTERNS.get("".join(sorted(on))) if not unsure else None
    return DecodedDigit(digit, "".join(sorted(on)), unsure)


def decode_number(image_bytes: bytes, digit_boxes: list[tuple], layout_text: str | None = None) -> str | None:
    """Digits left to right; returns None if any digit is undecided. With `layout_text` (e.g. Spark's
    '19.11'), the decoded digits are placed into the same layout (decimal point, sign), since SAM does not
    box decimal points. Abstains if the digit counts differ."""
    gray = _gray(image_bytes)
    cells = [decode_digit(gray, box).digit for box in sorted(digit_boxes, key=lambda b: b[0])]
    while cells and cells[0] == "":            # leading / trailing blank digit positions
        cells.pop(0)
    while cells and cells[-1] == "":
        cells.pop()
    if not cells or any(c is None or c == "" for c in cells):
        return None
    digits = cells
    if layout_text is None:
        return "".join(digits)
    slots = [i for i, ch in enumerate(layout_text) if ch.isdigit()]
    if len(slots) != len(digits):
        return None
    out = list(layout_text)
    for i, d in zip(slots, digits):
        out[i] = d
    return "".join(out)
