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
