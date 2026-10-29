"""Tiny synthetic videos for tests (no browser, no footage)."""

from pathlib import Path

import cv2
import numpy as np


def solid_states_video(path: Path, colors: list[tuple[int, int, int]], seconds_each: float = 1.0,
                       size: tuple[int, int] = (1400, 820), fps: int = 10) -> Path:
    """One solid colour per state (BGR), each held `seconds_each`; states differ, frames within a state don't."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for color in colors:
        frame = np.full((size[1], size[0], 3), color, dtype=np.uint8)
        for _ in range(round(seconds_each * fps)):
            writer.write(frame)
    writer.release()
    return path
