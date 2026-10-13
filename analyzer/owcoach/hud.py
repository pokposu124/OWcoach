"""HUD state check without AI: verifies model-claimed ability/ultimate use against the HUD pixels.

The model's reading of the HUD was the least reliable part of the first real runs (an ultimate
"used" while the gauge was still rising, a grenade "thrown" while it was on cooldown). This module
reads only a *state* per HUD slot — ready vs. not ready — from fixed screen regions, and checks
whether a ready -> not-ready transition happened near the time the model claims the ability was used.

Why state and not numbers: OCR (rapidocr) was tried on the cooldown / ultimate digits and missed
too many of them (italic HUD font, small size). A ready ability tile and the ready ultimate are
drawn as a bright white tile/disc, which separates cleanly from the cooldown state by the share of
near-white pixels. Calibrated on one 1280x720 recording with the default HUD layout; regions are
given in 1280x720 coordinates in config (hud_check.regions) and the frame is scaled to that size.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .media import MediaError, _require
from .timefmt import fmt_time

READY, NOT_READY = "ready", "not_ready"


@dataclass
class HudSample:
    time_seconds: float
    states: dict[str, str | None]  # slot -> ready / not_ready / None (HUD not visible)
    ratios: dict[str, float]


def _mask(region: dict, x0: int, y0: int, w: int, h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    xx, yy = xx + x0, yy + y0
    if region["shape"] == "circle":
        return (xx - region["cx"]) ** 2 + (yy - region["cy"]) ** 2 <= region["r"] ** 2
    return (xx >= region["x0"]) & (xx < region["x1"]) & (yy >= region["y0"]) & (yy < region["y1"])


def _bbox(regions: dict) -> tuple[int, int, int, int]:
    xs, ys = [], []
    for r in regions.values():
        if r["shape"] == "circle":
            xs += [r["cx"] - r["r"], r["cx"] + r["r"] + 1]
            ys += [r["cy"] - r["r"], r["cy"] + r["r"] + 1]
        else:
            xs += [r["x0"], r["x1"]]
            ys += [r["y0"], r["y1"]]
    x0, y0 = max(0, min(xs)), max(0, min(ys))
    return x0, y0, max(xs) - x0, max(ys) - y0


def sample_hud(video: Path, start: float, end: float, cfg: dict) -> list[HudSample]:
    """Read HUD slot states at cfg['fps'] between start and end (original-video seconds)."""
    regions = cfg["regions"]
    ref_w, ref_h = cfg["reference_resolution"]
    x0, y0, w, h = _bbox(regions)
    fps = float(cfg["fps"])
    start = max(0.0, start)
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{max(0.0, end - start):.3f}",
        "-vf", f"fps={fps},scale={ref_w}:{ref_h},crop={w}:{h}:{x0}:{y0}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"HUD frame extraction failed: {proc.stderr[-500:]!r}")
    frames = np.frombuffer(proc.stdout, np.uint8)
    if frames.size == 0:
        return []
    frames = frames.reshape(-1, h, w, 3)
    masks = {k: _mask(r, x0, y0, w, h) for k, r in regions.items()}
    tile_slots = cfg.get("hidden_check_slots") or []
    out = []
    for i, f in enumerate(frames):
        white = f.min(axis=2) > cfg["white_threshold"]
        ratios = {k: float(white[m].mean()) for k, m in masks.items()}
        blank = {s for s in tile_slots if ratios[s] < cfg["hidden_ratio"]}
        # all ability tiles blank -> HUD hidden (death cam, menu): every state unknown.
        # one tile blank -> that tile is flashing (it does so when it comes off cooldown): unknown.
        hidden = bool(tile_slots) and len(blank) == len(tile_slots)
        states = {k: None if hidden or k in blank else (READY if v >= cfg["ready_ratio"] else NOT_READY)
                  for k, v in ratios.items()}
        # fps filter output frame i covers [start + i/fps, ...); use the frame's start time
        out.append(HudSample(round(start + i / fps, 3), states, {k: round(v, 3) for k, v in ratios.items()}))
    return out


def _debounced(samples: list[HudSample], slot: str, min_run: int) -> list[tuple[float, str]]:
    """(time, state) where state is held for at least min_run consecutive known samples."""
    runs: list[list] = []  # [state, first_time, count]
    for s in samples:
        st = s.states.get(slot)
        if st is None:
            continue
        if runs and runs[-1][0] == st:
            runs[-1][2] += 1
        else:
            runs.append([st, s.time_seconds, 1])
    stable = [(t, st) for st, t, n in runs if n >= min_run]
    merged: list[tuple[float, str]] = []
    for t, st in stable:
        if not merged or merged[-1][1] != st:
            merged.append((t, st))
    return merged


def min_run_for(cfg: dict, slot: str) -> int:
    m = cfg["min_run_samples"]
    return int(m.get(slot, m["default"]) if isinstance(m, dict) else m)


def use_times(samples: list[HudSample], slot: str, min_run: int) -> list[float]:
    """Times where the slot changed from ready to not ready (= ability/ultimate used)."""
    seq = _debounced(samples, slot, min_run)
    return [t for (_, a), (t, b) in zip(seq, seq[1:]) if a == READY and b == NOT_READY]


def check_use_claim(claim_time: float, slot: str, samples: list[HudSample], cfg: dict) -> dict:
    """Compare a model claim "slot used at claim_time" with HUD transitions in the sampled window."""
    tol = float(cfg["search_seconds"])
    min_run = min_run_for(cfg, slot)
    near = [s for s in samples if abs(s.time_seconds - claim_time) <= tol]
    known = [s for s in near if s.states.get(slot) is not None]
    uses = use_times(samples, slot, min_run)
    hits = [t for t in uses if abs(t - claim_time) <= tol]
    if hits:
        t = min(hits, key=lambda x: abs(x - claim_time))
        return {"status": "verified", "hud_use_time_seconds": t,
                "note": f"HUD: {fmt_time(t)}에 준비 → 사용됨 전환 확인"}
    if not near or len(known) < max(min_run, len(near) // 2):
        return {"status": "unverifiable", "hud_use_time_seconds": None,
                "note": "해당 시각 전후 HUD가 가려졌거나 샘플이 부족함"}
    states = {s.states[slot] for s in known}
    if states == {READY}:
        what = "주장 시각 전후 계속 준비 상태(사용 안 됨)"
    elif states == {NOT_READY}:
        what = "주장 시각 전후 계속 쿨다운/충전 중(사용 가능 상태 아님)"
    else:
        what = "주장 시각 전후에 준비 → 사용됨 전환이 없음"
    other = [t for t in uses if abs(t - claim_time) > tol]
    if other:
        what += f"; 이 구간에서 HUD상 사용 감지 시각: {', '.join(fmt_time(t) for t in other)}"
    return {"status": "contradicted", "hud_use_time_seconds": None, "note": what}


def detect_buff_target(video: Path, use_time: float, slots: list[dict], cfg: dict) -> dict:
    """Which team-list slot shows the buff arrow animation right after a buff (e.g. Nano Boost) was used.

    slots: [{"slot": n, "x": portrait_left_x, "player_name": ..., "hero": ...}] — candidate ally slots.
    The arrow is drawn in white over the upper part of the receiving ally's portrait for a few seconds.
    Each slot's white-pixel share in that zone is compared with its own baseline just before the use;
    frames where most slots brighten together (screen flash) are ignored.

    No arrow but one ally's portrait turns dark and stays dark (that ally died) -> "interrupted":
    the buff was cut off, e.g. a late Nano Boost on an ally who was already going down.
    """
    if not slots:
        return {"status": "unverifiable", "note": "대상 후보(아군 칸)가 없음"}
    fps = float(cfg["fps"])
    y0, y1 = min(cfg["y"], cfg["death_y"]), max(cfg["y"] + cfg["h"], cfg["death_y"] + cfg["death_h"])
    xs = [s["x"] for s in slots]
    x_lo, x_hi = min(xs) + cfg["dx0"], max(xs) + cfg["dx1"]
    start = max(0.0, use_time - cfg["baseline_seconds"])
    ref_w, ref_h = cfg["reference_resolution"]
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}", "-i", str(video),
        "-t", f"{use_time + cfg['window_seconds'] - start:.3f}",
        "-vf", f"fps={fps},scale={ref_w}:{ref_h},crop={x_hi - x_lo}:{y1 - y0}:{x_lo}:{y0}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"buff arrow extraction failed: {proc.stderr[-300:]!r}")
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, y1 - y0, x_hi - x_lo, 3)
    ay0, dy0 = cfg["y"] - y0, cfg["death_y"] - y0
    white = frames[:, ay0:ay0 + cfg["h"]].min(axis=3) > cfg["white_threshold"]
    ratios = np.array([[w[:, s["x"] + cfg["dx0"] - x_lo:s["x"] + cfg["dx1"] - x_lo].mean() for s in slots] for w in white])
    portrait = frames[:, dy0:dy0 + cfg["death_h"]].astype(np.float32).mean(axis=3)
    lum = np.array([[p[:, s["x"] - x_lo:s["x"] - x_lo + cfg["death_w"]].mean() for s in slots] for p in portrait])
    times = start + np.arange(len(ratios)) / fps
    before, after = ratios[times < use_time - 0.25], ratios[times >= use_time]
    if len(before) == 0 or len(after) == 0:
        return {"status": "unverifiable", "note": "대상 판별용 프레임 부족"}
    rise = after - np.median(before, axis=0) >= cfg["min_increase"]
    flash = rise.sum(axis=1) > len(slots) / 2
    hits = rise[~flash].sum(axis=0)
    order = np.argsort(hits)[::-1]
    best = int(order[0])
    second = int(hits[order[1]]) if len(order) > 1 else 0
    if hits[best] >= cfg["min_frames"] and second * 2 <= hits[best]:
        s = slots[best]
        return {"status": "verified", "slot": s["slot"], "player_name": s["player_name"], "hero": s.get("hero"),
                "arrow_frames": int(hits[best]),
                "note": f"팀 목록 {s['slot']}번 칸({s['player_name']})에 강화 화살표 {int(hits[best])}프레임"}
    # no arrow: did an ally go down right after (portrait stays darker until the end of the window)?
    tail = lum[times >= times[-1] - cfg["death_hold_seconds"]]
    base = np.median(lum[times < use_time - 0.25], axis=0)
    drop = 1 - np.median(tail, axis=0) / np.maximum(base, 1e-6)
    dead = [i for i in range(len(slots)) if drop[i] >= cfg["death_min_drop"]]
    if len(dead) == 1:
        s = slots[dead[0]]
        return {"status": "interrupted", "slot": s["slot"], "player_name": s["player_name"], "hero": s.get("hero"),
                "note": f"강화 화살표 없음, 직후 {s['slot']}번 칸({s['player_name']}) 초상화가 어두워짐"
                        f"(밝기 {drop[dead[0]] * 100:.0f}% 감소): 대상이 쓰러져 효과가 끊긴 것으로 추정"}
    if len(dead) > 1:
        names = ", ".join(slots[i]["player_name"] for i in dead)
        return {"status": "unverifiable", "note": f"강화 화살표 없음, 아군 여러 명이 쓰러짐({names}): 대상 특정 불가"}
    return {"status": "unverifiable", "note": "어느 아군 칸에도 강화 화살표가 뚜렷하지 않음(화면 가림 등)"}


def scan_uses(video: Path, duration: float, slot: str, cfg: dict) -> list[float]:
    """All ready -> used transitions of one HUD slot over the whole video (e.g. every ultimate)."""
    return use_times(sample_hud(video, 0.0, duration, cfg), slot, min_run_for(cfg, slot))


def scan_player_deaths(video: Path, duration: float, cfg: dict) -> list[float]:
    """Start times of the player's deaths (see scan_dead_periods)."""
    return [start for start, _ in scan_dead_periods(video, duration, cfg)]


def scan_dead_periods(video: Path, duration: float, cfg: dict) -> list[tuple[float, float]]:
    """(start, end) of stretches where the player's health bar is empty (health 0) for >= min_seconds.

    While dead, the HUD stays on screen (spectating from the death spot) but the ten health blocks
    under the health number go dark and a red skull covers the portrait, until respawn (~10 s).
    cfg: death_check.health_bar (region in 1280x720, white_threshold, empty_ratio, fps, min_seconds).
    """
    hb = cfg["health_bar"]
    w, h = hb["x1"] - hb["x0"], hb["y1"] - hb["y0"]
    ref_w, ref_h = hb["reference_resolution"]
    fps = float(hb["fps"])
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-i", str(video), "-t", f"{duration:.3f}",
        "-vf", f"fps={fps},scale={ref_w}:{ref_h},crop={w}:{h}:{hb['x0']}:{hb['y0']}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"health bar extraction failed: {proc.stderr[-300:]!r}")
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, h, w, 3)
    white = (frames.min(axis=3) > hb["white_threshold"]).mean(axis=(1, 2))
    empty = white < hb["empty_ratio"]
    out, start = [], None
    for i, e in enumerate([*empty, False]):
        if e and start is None:
            start = i
        if not e and start is not None:
            if (i - start) / fps >= hb["min_seconds"]:
                out.append((round(start / fps, 2), round(i / fps, 2)))
            start = None
    return out


def detect_status_icon(video: Path, use_time: float, slots: list[dict], cfg: dict) -> dict:
    """Which enemy team-list slot shows a status icon right after a disabling skill was used.

    Sleep Dart: a white "zz" appears over the upper-right of the slept enemy's portrait for a few
    seconds. Per slot, frames whose white share in that zone rises over the slot's own baseline are
    counted. One slot clearly ahead -> hit on that enemy; no slot rising at all -> missed.
    """
    if not slots:
        return {"status": "unverifiable", "note": "대상 후보(적 칸)가 없음"}
    fps = float(cfg["fps"])
    y0, y1 = cfg["y"], cfg["y"] + cfg["h"]
    xs = [s["x"] for s in slots]
    x_lo, x_hi = min(xs) + cfg["dx0"], max(xs) + cfg["dx1"]
    start = max(0.0, use_time - cfg["baseline_seconds"])
    ref_w, ref_h = cfg["reference_resolution"]
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}", "-i", str(video),
        "-t", f"{use_time + cfg['window_seconds'] - start:.3f}",
        "-vf", f"fps={fps},scale={ref_w}:{ref_h},crop={x_hi - x_lo}:{y1 - y0}:{x_lo}:{y0}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"status icon extraction failed: {proc.stderr[-300:]!r}")
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, y1 - y0, x_hi - x_lo, 3)
    white = frames.min(axis=3) > cfg["white_threshold"]
    ratios = np.array([[w[:, s["x"] + cfg["dx0"] - x_lo:s["x"] + cfg["dx1"] - x_lo].mean() for s in slots] for w in white])
    times = start + np.arange(len(ratios)) / fps
    before, after = ratios[times < use_time], ratios[times >= use_time]
    if len(before) == 0 or len(after) == 0:
        return {"status": "unverifiable", "note": "판별용 프레임 부족"}
    hits = ((after - np.median(before, axis=0)) >= cfg["min_increase"]).sum(axis=0)
    order = np.argsort(hits)[::-1]
    best = int(order[0])
    second = int(hits[order[1]]) if len(order) > 1 else 0
    if hits[best] >= cfg["min_frames"] and second * 2 <= hits[best]:
        s = slots[best]
        return {"status": "verified", "slot": s["slot"], "player_name": s["player_name"], "hero": s.get("hero"),
                "icon_frames": int(hits[best]),
                "note": f"명중: 팀 목록 {s['slot']}번 칸({s['player_name']})에 {cfg.get('icon_name', '상태 표시')} {int(hits[best])}프레임"}
    if hits.max() == 0:
        return {"status": "missed", "note": f"어느 적 칸에도 {cfg.get('icon_name', '상태 표시')}가 뜨지 않음: 빗나감"}
    return {"status": "unverifiable", "note": "표시가 약하거나 여러 칸에 걸쳐 있어 대상 특정 불가"}


def detect_reflect(video: Path, use_time: float, cfg: dict) -> dict:
    """Was the player's own skill reflected back at them (e.g. into an enemy Genji's deflect)?

    Biotic Grenade: when it comes back, the player gets its anti-heal. The icon left of the player's
    health number turns from a white "+" into a purple broken-heal icon for a few seconds. Enemy
    ultimate effects can tint that spot purple too, but only for a moment (<= 0.25 s measured), so the
    icon must stay for min_seconds, starting within onset_before/onset_after of the HUD use time.
    cfg: hud_check.self_antiheal (region in reference_resolution, fps, purple test, min_ratio, ...).
    """
    fps = float(cfg["fps"])
    w, h = cfg["x1"] - cfg["x0"], cfg["y1"] - cfg["y0"]
    ref_w, ref_h = cfg["reference_resolution"]
    start = max(0.0, use_time - cfg["onset_before_seconds"])
    span = cfg["onset_before_seconds"] + cfg["onset_after_seconds"] + cfg["min_seconds"] + 0.5
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{span:.3f}",
        "-vf", f"fps={fps},scale={ref_w}:{ref_h},crop={w}:{h}:{cfg['x0']}:{cfg['y0']}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"reflect check extraction failed: {proc.stderr[-300:]!r}")
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, h, w, 3).astype(np.int16)
    if len(frames) == 0:
        return {"status": "unverifiable", "note": "판별용 프레임 부족"}
    r, g, b = frames[..., 0], frames[..., 1], frames[..., 2]
    on = ((b > 150) & (r > 110) & (b - g > 70) & (r - g > 30)).mean(axis=(1, 2)) >= cfg["min_ratio"]
    times = start + np.arange(len(on)) / fps
    need = max(1, int(round(cfg["min_seconds"] * fps)))
    run = 0
    for i, x in enumerate(on):
        run = run + 1 if x else 0
        if run < need:
            continue
        onset = times[i - run + 1]
        if onset <= use_time + cfg["onset_after_seconds"]:
            return {"status": "reflected", "onset_seconds": round(float(onset), 2),
                    "note": f"반사됨: {fmt_time(float(onset))}부터 자신에게 {cfg['icon_name']} {cfg['min_seconds']:g}초 이상 유지"}
    return {"status": "not_reflected", "note": f"사용 직후 자신에게 {cfg['icon_name']} 없음: 반사되지 않음"}


def player_team_color(video: Path, duration: float, cfg: dict) -> dict:
    """Ally color of this video from the border of the player's own portrait (bottom left).

    Team colors follow the user's settings / the viewer (the player's team was red in both Ana videos),
    so "red = enemy" cannot be assumed. The portrait border always has the ally color.
    cfg: team.player_portrait (crop in reference_resolution, frames, min_saturation, min_value, min_gap).
    """
    ref_w, ref_h = cfg["reference_resolution"]
    w, h = cfg["w"], cfg["h"]
    rgbs = []
    for i in range(cfg["frames"]):
        t = duration * (i + 1) / (cfg["frames"] + 1)
        proc = subprocess.run([
            _require("ffmpeg"), "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", f"scale={ref_w}:{ref_h},crop={w}:{h}:{cfg['x0']}:{cfg['y0']}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], capture_output=True)
        if proc.returncode != 0 or len(proc.stdout) != w * h * 3:
            continue
        a = np.frombuffer(proc.stdout, np.uint8).reshape(h, w, 3).astype(float) / 255
        mx, mn = a.max(axis=2), a.min(axis=2)
        m = ((mx - mn) / (mx + 1e-6) > cfg["min_saturation"]) & (mx > cfg["min_value"])
        if m.sum() >= 50:
            rgbs.append([a[..., c][m].mean() for c in range(3)])
    if not rgbs:
        return {"ally_color": None, "note": "초상화 테두리 색을 측정하지 못함"}
    r, g, b = np.median(np.array(rgbs), axis=0)
    color = "빨강" if r - b >= cfg["min_gap"] else ("파랑" if b - r >= cfg["min_gap"] else None)
    return {"ally_color": color, "rgb": [round(float(x), 2) for x in (r, g, b)], "frames": len(rgbs),
            "note": f"좌하단 내 초상화 테두리 평균색 RGB({r:.2f}, {g:.2f}, {b:.2f})"}

