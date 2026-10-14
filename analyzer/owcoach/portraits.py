"""Hero identification from the team-list portraits by image matching (no AI).

The model could not identify heroes from the team list reliably: it named MMG (Doctrine) as
Mauga in one run and Sombra in the next, both "high" confidence, even with a portrait sheet
attached. The team-list portrait is a crop of the hero-select portrait, so each slot's portrait
patch is matched against every reference portrait by normalized cross-correlation over a small
range of scales and positions. On the sample video the three known heroes (Ana, Doctrine, D.Va)
ranked first with scores 0.77-0.93 vs. 0.46-0.68 for the runner-up.

Reference portraits: config/hero_portraits/portraits.yaml (sheet image + one box per hero).
Slot geometry: config team.portrait_slots, in 1280x720 coordinates.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

from .config import ANALYZER_ROOT, load_yaml
from .media import MediaError, _require


def load_portraits(path: str) -> dict:
    meta_path = ANALYZER_ROOT / path
    meta = load_yaml(meta_path)
    sheet_path = meta_path.parent / meta["sheet"]
    sheet = Image.open(sheet_path).convert("RGB")
    heroes = [(h["name"], sheet.crop(tuple(h["box"]))) for h in meta["heroes"]]
    return {"heroes": heroes, "source": meta.get("source"), "sheet": str(sheet_path.relative_to(ANALYZER_ROOT)),
            "sheet_bytes": sheet_path.read_bytes()}


def read_frame(video: Path, timestamp: float, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1",
        "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) != w * h * 3:
        raise MediaError(f"frame read failed at {timestamp}s: {proc.stderr[-300:]!r}")
    return np.frombuffer(proc.stdout, np.uint8).reshape(h, w, 3).astype(np.float32)


def _center(x: np.ndarray) -> np.ndarray:
    return x - x.mean(axis=(-3, -2, -1), keepdims=True)


def slot_scores(frame: np.ndarray, slots: dict, refs: list[tuple[str, Image.Image]], scales: list[float]) -> np.ndarray:
    """(n_slots, n_heroes) best NCC of each slot patch against each reference over scales/positions."""
    h, w, y = slots["h"], slots["w"], slots["y"]
    xs = slots["left_x"] + slots["right_x"]
    patches = np.stack([_center(frame[y:y + h, x:x + w]) for x in xs])          # (S,h,w,3)
    pnorm = np.sqrt((patches ** 2).sum(axis=(1, 2, 3)))                        # (S,)
    flat = patches.reshape(len(xs), -1)                                         # (S,h*w*3)
    out = np.full((len(xs), len(refs)), -1.0)
    for j, (_, img) in enumerate(refs):
        for s in scales:
            tw, th = int(img.width * s), int(img.height * s)
            if th < h or tw < w:
                continue
            t = np.asarray(img.resize((tw, th), Image.BILINEAR), dtype=np.float32)
            win = _center(sliding_window_view(t, (h, w, 3))[:, :, 0])            # (ny,nx,h,w,3)
            wflat = win.reshape(-1, h * w * 3)                                  # (N,h*w*3)
            wnorm = np.sqrt((wflat ** 2).sum(axis=1))                           # (N,)
            ncc = (flat @ wflat.T) / (pnorm[:, None] * wnorm[None, :] + 1e-6)   # (S,N)
            out[:, j] = np.maximum(out[:, j], ncc.max(axis=1))
    return out


def identify(video: Path, times: list[float], cfg: dict, refs: list[tuple[str, Image.Image]]) -> list[dict]:
    """Per team-list slot (left to right, 1..10): hero, score, margin to runner-up, confidence.

    Scores are the best over the given frames: a dead player's portrait is greyed out, so averaging
    would penalise a slot for frames in which it simply cannot be read."""
    slots = cfg["portrait_slots"]
    scales = list(np.round(np.arange(slots["scale_min"], slots["scale_max"] + 1e-9, slots["scale_step"]), 3))
    size = tuple(cfg["reference_resolution"])
    scores = np.max([slot_scores(read_frame(video, t, size), slots, refs, scales) for t in times], axis=0)
    th = slots["thresholds"]
    out = []
    for i, row in enumerate(scores):
        order = np.argsort(row)[::-1]
        best, second = float(row[order[0]]), float(row[order[1]])
        margin = best - second
        if best >= th["high_score"] and margin >= th["high_margin"]:
            conf = "high"
        elif best >= th["min_score"] and margin >= th["min_margin"]:
            conf = "medium"
        else:
            conf = "low"
        out.append({
            "slot": i + 1, "side": "left" if i < len(slots["left_x"]) else "right",
            "hero": refs[order[0]][0] if conf != "low" else "불명",
            "best_match": refs[order[0]][0], "score": round(best, 3),
            "runner_up": refs[order[1]][0], "margin": round(margin, 3), "confidence": conf,
        })
    return out


def _labels(frame: np.ndarray, slots: dict, refs: list[tuple[str, Image.Image]], scales: list[float]) -> list[str | None]:
    """Hero per slot in one frame, None where the match is not clear (dead / greyed portrait, effects)."""
    th = slots["thresholds"]
    out: list[str | None] = []
    for row in slot_scores(frame, slots, refs, scales):
        order = np.argsort(row)[::-1]
        best, margin = float(row[order[0]]), float(row[order[0]] - row[order[1]])
        out.append(refs[order[0]][0] if best >= th["min_score"] and margin >= th["min_margin"] else None)
    return out


def roster_timeline(video: Path, duration: float, cfg: dict, refs: list[tuple[str, Image.Image]]) -> dict:
    """Hero changes per team-list slot over the whole video.

    Frames every `timeline.step_seconds`; a hero counts for a slot only when `timeline.min_run`
    consecutive frames agree (single-frame misreads were seen: a dead player's greyed portrait, effects).
    Each swap time is then narrowed down between the two reads with frames every `timeline.refine_seconds`.
    Returns {"segments": {slot: [{hero, start_seconds, end_seconds}]}, "swaps": [{slot, from_hero, to_hero, time_seconds}]}.
    """
    slots, tl = cfg["portrait_slots"], cfg["portrait_slots"]["timeline"]
    scales = list(np.round(np.arange(slots["scale_min"], slots["scale_max"] + 1e-9, slots["scale_step"]), 3))
    size = tuple(cfg["reference_resolution"])
    xs = slots["left_x"] + slots["right_x"]
    step = float(tl["step_seconds"])
    times = list(np.round(np.arange(step / 2, duration, step), 2))
    reads = [_labels(read_frame(video, t, size), slots, refs, scales) for t in times]
    segments: dict[int, list[dict]] = {}
    swaps: list[dict] = []
    for i in range(len(xs)):
        seq = [(t, r[i]) for t, r in zip(times, reads)]
        runs: list[dict] = []
        j = 0
        while j < len(seq):
            k = j
            while k + 1 < len(seq) and seq[k + 1][1] == seq[j][1]:
                k += 1
            if seq[j][1] and k - j + 1 >= tl["min_run"]:
                if runs and runs[-1]["hero"] == seq[j][1]:
                    runs[-1]["end_seconds"] = float(seq[k][0])
                else:
                    runs.append({"hero": seq[j][1], "start_seconds": float(seq[j][0]), "end_seconds": float(seq[k][0])})
            j = k + 1
        one = {"left_x": [xs[i]], "right_x": [], **{k: v for k, v in slots.items() if k not in ("left_x", "right_x")}}
        for a, b in zip(runs, runs[1:]):
            # first frame between the two reads that already shows the new hero
            t_swap = b["start_seconds"]
            for t in np.arange(a["end_seconds"] + tl["refine_seconds"], b["start_seconds"], tl["refine_seconds"]):
                if _labels(read_frame(video, float(t), size), one, refs, scales)[0] == b["hero"]:
                    t_swap = float(t)
                    break
            b["start_seconds"] = round(t_swap, 2)
            swaps.append({"slot": i + 1, "from_hero": a["hero"], "to_hero": b["hero"], "time_seconds": round(t_swap, 2)})
        if runs:
            runs[0]["start_seconds"] = 0.0
            runs[-1]["end_seconds"] = round(duration, 2)
        segments[i + 1] = runs
    return {"segments": segments, "swaps": sorted(swaps, key=lambda w: w["time_seconds"])}

