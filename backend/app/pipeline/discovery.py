"""Display discovery per clip: SAM 3.1 finds the displays; the registry says which is which.

With SEG_PROVIDER=meta, in order of preference:
  1. TRACKING (SAM_TRACK=true): one SAM 3.1 video call returns every display's box in every frame, with a
     stable object id. Handheld phone footage needs this: the display drifts and zooms in the frame
     (Test 2.mp4: a fixed box made a static display look "changed" on every frame).
  2. single-image discovery on the first frame (if tracking fails or is off)
  3. the display's manual ROI (if SAM fails or misses it); no region at all -> the display is skipped
Matching SAM objects to registered displays (per SAM prompt), using each object's FIRST appearance:
  - displays with a manual ROI take the detection that overlaps it most (IoU >= 0.3)
  - the rest go left to right by layout_rank (most confident detections first when SAM finds extras)
Results are stored per clip in clip_rois (track = box per sampled frame), because framing changes.
"""

import logging
from collections import defaultdict
from pathlib import Path

import cv2

from ..jobs.events import emit
from ..llm import CacheMiss, ModelUnavailable
from ..llm.sam import Detection, iou
from .calls import record_call
from .ingest import SampledFrame, scale_roi

log = logging.getLogger(__name__)
MIN_IOU = 0.3
MAX_TRACK_GAP_S = 1.0            # beyond this from any tracked frame, keep the last known box


def _xyxy(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = box
    return (x, y, x + w, y + h)


def match_detections(displays: list[dict], detections: list[Detection], frame_size: tuple[int, int]) -> dict:
    """{display_id: Detection} for one SAM prompt's displays (pure function)."""
    remaining = sorted(detections, key=lambda d: -(d.confidence or 0))
    matches: dict = {}
    for display in displays:
        if not display.get("roi"):
            continue
        manual = _xyxy(scale_roi(display["roi"], display.get("roi_frame_size"), frame_size))
        best = max(remaining, key=lambda d: iou(d.box, manual), default=None)
        if best is not None and iou(best.box, manual) >= MIN_IOU:
            matches[display["display_id"]] = best
            remaining.remove(best)
    unmatched = sorted((d for d in displays if d["display_id"] not in matches),
                       key=lambda d: (d.get("layout_rank") is None, d.get("layout_rank") or 0, d["display_id"]))
    chosen = sorted(remaining[:len(unmatched)], key=lambda d: d.box[0])          # left to right
    for display, detection in zip(unmatched, chosen):
        if display.get("layout_rank") is not None or len(unmatched) == 1:
            matches[display["display_id"]] = detection
    return matches


def boxes_per_frame(track: list[Detection], frames: list[SampledFrame], fps: float) -> dict[int, tuple]:
    """{sampled frame index: (x, y, w, h)} from one object's tracked detections (nearest source frame)."""
    by_frame = {d.frame: d for d in sorted(track, key=lambda d: d.confidence or 0)}    # keep the best per frame
    if not by_frame:
        return {}
    keys = sorted(by_frame)
    out, last = {}, by_frame[keys[0]].xywh
    for f in frames:
        src = round(f.t_offset_s * fps)
        nearest = min(keys, key=lambda k: abs(k - src))
        if abs(nearest - src) <= MAX_TRACK_GAP_S * fps:
            last = by_frame[nearest].xywh
        out[f.index] = last
    return out


async def _track(ctx, video_path: Path, frames: list[SampledFrame], group: list[dict], prompt: str):
    """Tracked boxes for this prompt's displays: ({display_id: {frame_index: box}}, [unassigned], meta) or None."""
    try:
        detections, meta = await ctx.sam.track_video(video_path, prompt)
    except (ModelUnavailable, CacheMiss) as exc:
        log.warning("SAM tracking failed for %r: %s; trying single-image discovery", prompt, exc)
        return None
    await record_call(ctx.pool, meta)
    by_object: dict = defaultdict(list)
    for d in detections:
        by_object[d.object_id].append(d)
    if not by_object:
        return None
    first_seen = [min(ds, key=lambda d: d.frame) for ds in by_object.values()]
    matches = match_detections(group, first_seen, frames[0].size)
    fps = cv2.VideoCapture(str(video_path)).get(cv2.CAP_PROP_FPS) or 30.0
    tracks = {display_id: boxes_per_frame(by_object[det.object_id], frames, fps) for display_id, det in matches.items()}
    assigned = {det.object_id for det in matches.values()}
    hits = {display_id: det for display_id, det in matches.items()}
    unassigned = [{"box": d.xywh, "confidence": d.confidence, "prompt": prompt} for d in first_seen
                  if d.object_id not in assigned]
    return tracks, hits, unassigned, meta


async def _detect_once(ctx, first: SampledFrame, group: list[dict], prompt: str):
    try:
        detections, meta = await ctx.sam.segment_image(first.jpeg, prompt, mime="image/jpeg")
    except (ModelUnavailable, CacheMiss) as exc:
        log.warning("SAM discovery failed for %r: %s; using manual ROIs", prompt, exc)
        return None
    await record_call(ctx.pool, meta)
    matches = match_detections(group, detections, first.size)
    assigned = {id(d) for d in matches.values()}
    unassigned = [{"box": d.xywh, "confidence": d.confidence, "prompt": prompt} for d in detections if id(d) not in assigned]
    return matches, unassigned, meta


async def _detect_each(ctx, frames: list[SampledFrame], group: list[dict], prompt: str):
    """Photos: each one is framed differently, so the displays are found on every photo (one SAM call each).
    ({display_id: {frame_index: box}}, {display_id: first hit}, unassigned, meta) or None."""
    tracks: dict = defaultdict(dict)
    hits, unassigned, meta = {}, [], None
    for frame in frames:
        once = await _detect_once(ctx, frame, group, prompt)
        if once is None:
            continue
        matches, extra, meta = once
        for display_id, det in matches.items():
            tracks[display_id][frame.index] = det.xywh
            hits.setdefault(display_id, det)
        unassigned += extra if frame is frames[0] else []
    if not tracks:
        return None
    for display_id, track in tracks.items():          # a photo where a display wasn't found keeps the nearest box
        known = sorted(track)
        for frame in frames:
            if frame.index not in track:
                track[frame.index] = track[min(known, key=lambda k: abs(k - frame.index))]
    return dict(tracks), hits, unassigned, meta


async def resolve_rois(ctx, clip_id: str, displays: list[dict], frames: list[SampledFrame],
                       video_path: Path | None = None, stills: bool = False) -> dict[str, dict[int, tuple]]:
    """{display_id: {sampled frame index: (x, y, w, h)}} for this clip. stills=True: photos, detected one by one."""
    pool = ctx.pool
    stored = {r["display_id"]: r for r in await pool.fetch("SELECT * FROM clip_rois WHERE clip_id = $1", clip_id)}
    if stored and len(stored) == len(displays):                        # a retried job reuses its regions
        out = {}
        for display_id, r in stored.items():
            static = (r["roi"]["x"], r["roi"]["y"], r["roi"]["w"], r["roi"]["h"])
            track = {int(k): tuple(v) for k, v in (r["track"] or {}).items()}
            out[display_id] = {f.index: track.get(f.index, static) for f in frames}
        return out

    first = frames[0]
    regions: dict = {}           # display_id -> {"static": box, "track": {...} | None, "hit": {...}}
    unassigned: list = []
    tracked_any = False
    if ctx.settings.seg_provider == "meta" and ctx.sam is not None:
        for prompt in sorted({d["sam_prompt"] or "digital display" for d in displays}):
            group = [d for d in displays if (d["sam_prompt"] or "digital display") == prompt]
            result = None
            if stills and len(frames) > 1:
                result = await _detect_each(ctx, frames, group, prompt)
            elif ctx.settings.sam_track and video_path is not None and hasattr(ctx.sam, "track_video"):
                result = await _track(ctx, video_path, frames, group, prompt)
            if result is not None:
                tracks, hits, extra, meta = result
                tracked_any = True
                for display_id, track in tracks.items():
                    det = hits[display_id]
                    regions[display_id] = {"static": track[first.index], "track": track,
                                           "hit": {"confidence": det.confidence,
                                                   "sam_object_id": getattr(det, "object_id", None),
                                                   "model_call_id": meta.call_id}}
                unassigned += extra
                continue
            once = await _detect_once(ctx, first, group, prompt)
            if once is not None:
                matches, extra, meta = once
                for display_id, det in matches.items():
                    regions[display_id] = {"static": det.xywh, "track": None,
                                           "hit": {"confidence": det.confidence, "sam_object_id": det.object_id,
                                                   "model_call_id": meta.call_id}}
                unassigned += extra

    out: dict = {}
    for display in displays:
        display_id = display["display_id"]
        region = regions.get(display_id)
        if region is None:
            if not display.get("roi"):
                continue
            region = {"static": scale_roi(display["roi"], display.get("roi_frame_size"), first.size), "track": None,
                      "hit": {}}
            source = "manual"
        else:
            source = "sam"
        x, y, w, h = region["static"]
        track = region["track"]
        await pool.execute(
            """INSERT INTO clip_rois (clip_id, display_id, roi, frame_size, source, confidence, sam_object_id,
                                      model_call_id, track)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (clip_id, display_id) DO NOTHING""",
            clip_id, display_id, {"x": x, "y": y, "w": w, "h": h}, {"w": first.size[0], "h": first.size[1]}, source,
            region["hit"].get("confidence"), region["hit"].get("sam_object_id"), region["hit"].get("model_call_id"),
            {str(k): list(v) for k, v in track.items()} if track else None)
        out[display_id] = dict(track) if track else {f.index: region["static"] for f in frames}
        region["source"] = source

    await emit(pool, clip_id, "displays_detected", {
        "seg_provider": ctx.settings.seg_provider, "tracked": tracked_any, "frame_size": list(first.size),
        "displays": [{"display_id": d, "box": list(r["static"]), "source": r.get("source", "sam"),
                      "tracked": r["track"] is not None} for d, r in regions.items()]
        + [{"display_id": d["display_id"], "box": list(out[d["display_id"]][first.index]), "source": "manual",
            "tracked": False} for d in displays if d["display_id"] in out and d["display_id"] not in regions],
        "unassigned_detections": unassigned})
    return out
