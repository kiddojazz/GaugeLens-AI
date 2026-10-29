"""Seven-segment decoder on synthetic digit cells: lit segments dark, unlit ones as faint or strong ghosts."""

import io

from PIL import Image, ImageDraw

from backend.app.pipeline.segments import decode_digit, decode_number, _gray

W, H, T = 60, 100, 10          # cell size and segment thickness
SEG_RECTS = {"a": (8, 0, 52, T), "g": (8, 45, 52, 45 + T), "d": (8, 90, 52, 100),
             "f": (0, 6, T, 48), "b": (50, 6, 60, 48), "e": (0, 52, T, 94), "c": (50, 52, 60, 94)}
DIGITS = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg",
          "7": "abc", "8": "abcdefg", "9": "abcdfg"}
BACKGROUND, LIT = 160, 25


def cells_image(patterns: list[str], ghost: int = 8, gap: int = 20) -> tuple[bytes, list[tuple]]:
    """One full digit cell per pattern; unlit segments drawn `ghost` grey levels darker than background."""
    image = Image.new("L", (len(patterns) * (W + gap) + gap, H + 2 * gap), BACKGROUND)
    draw = ImageDraw.Draw(image)
    boxes = []
    for i, pattern in enumerate(patterns):
        ox, oy = gap + i * (W + gap), gap
        for name, (x1, y1, x2, y2) in SEG_RECTS.items():
            draw.rectangle((ox + x1, oy + y1, ox + x2 - 1, oy + y2 - 1), fill=LIT if name in pattern else BACKGROUND - ghost)
        boxes.append((ox, oy, ox + W, oy + H))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), boxes


def test_every_digit_decodes_under_faint_and_strong_ghosts():
    for ghost in (6, 25):
        png, boxes = cells_image(list(DIGITS.values()), ghost=ghost)
        gray = _gray(png)
        assert [decode_digit(gray, b).digit for b in boxes] == list(DIGITS), ghost


def test_a_seven_with_its_upper_left_segment_lit_is_still_a_seven():
    png, boxes = cells_image(["abcf", "abcdfg", "abcfg"])          # hooked 7, 9, 9 without its bottom
    gray = _gray(png)
    assert [decode_digit(gray, b).digit for b in boxes] == ["7", "9", "9"]


def test_numbers_keep_the_readers_layout_and_skip_blank_positions():
    png, boxes = cells_image(["", "bc", "abcf", "bc", "bc"])        # blank cell, then 1 7 1 1
    assert decode_number(png, boxes) == "1711"
    assert decode_number(png, boxes, layout_text="19.11") == "17.11"   # decimal point from the reader
    assert decode_number(png, boxes, layout_text="1911") == "1711"
    assert decode_number(png, boxes, layout_text="911") is None        # digit counts differ: abstain


def test_a_thin_lit_box_is_a_one_and_tiny_boxes_abstain():
    png, _ = cells_image(["bc"])
    gray = _gray(png)
    assert decode_digit(gray, (20 + 50, 20, 20 + 60, 120)).digit == "1"   # SAM boxed only the lit bar
    assert decode_digit(gray, (20, 20, 30, 30)).digit is None


def test_a_blank_gap_inside_a_number_makes_it_abstain():
    png, boxes = cells_image(["bc", "", "bc"])
    assert decode_number(png, boxes) is None
