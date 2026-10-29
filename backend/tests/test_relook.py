"""Digit grouping and re-read selection (pure functions), on real SAM 3.1 digit boxes from Spike C."""

from backend.app.llm.sam import Detection
from backend.app.pipeline.relook import NumberRead, choose_read, field_box, group_digits

FRAME = (1400, 820)
# SAM 3.1 'number' on the mock panel OUTPUT page (Oct 26): three W digits, three VA digits, clamp digits
REAL_DIGITS = [(626, 294, 687, 403), (312, 421, 337, 466), (710, 300, 778, 397), (346, 421, 374, 466),
               (1039, 336, 1073, 386), (384, 421, 412, 466), (537, 294, 598, 403), (1109, 339, 1115, 386),
               (1021, 338, 1027, 386)]


def dets(boxes):
    return [Detection(i, 0, b, FRAME, 0.8) for i, b in enumerate(boxes)]


def test_real_sam_digits_group_into_the_three_numbers_on_screen():
    groups = sorted(group_digits(dets(REAL_DIGITS)), key=lambda g: g.box[0])
    assert [len(g.digits) for g in groups] == [3, 3, 3]
    va, watts, clamp = groups
    assert va.box == (312, 421, 412, 466) and watts.box == (537, 294, 778, 403)
    assert clamp.box[0] == 1021 and clamp.box[2] == 1115                  # thin 1s joined across the decimal gap


def test_field_box_leaves_room_for_the_unit_and_stays_in_bounds():
    watts = next(g for g in group_digits(dets(REAL_DIGITS)) if g.box[0] == 537)
    x, y, w, h = field_box(watts, FRAME)
    assert x < 537 and x + w > 778 + 60                                    # the 'W' to the right is included
    x, y, w, h = field_box(watts, (800, 420))
    assert x + w <= 800 and y + h <= 420


def test_rows_of_different_height_stay_separate():
    groups = group_digits(dets([(0, 0, 60, 100), (70, 0, 130, 100), (140, 40, 160, 70)]))
    assert [len(g.digits) for g in groups] == [2, 1]


def read(image, value, unit, raw):
    return NumberRead(image=image, value=value, unit=unit, raw_text=raw, legible=True)


def test_the_re_read_is_chosen_by_unit_then_digit_count():
    reads = [read(1, 338, "W", "338"), read(2, 393, "VA", "393"), read(3, 11, "%", "11")]
    assert choose_read(reads, "W", "838").value == 338
    assert choose_read(reads, "%", "11").value == 11
    assert choose_read([read(1, 23.6, "V", "23.6"), read(2, 230, "V", "230")], "V", "47.2").value == 23.6
    assert choose_read(reads, "Hz", "50.0") is None                        # no unit match: don't guess
    assert choose_read([read(1, 17.1, None, "17.1")], "A", "19.1").value == 17.1   # single number: take it
