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


def _frames(video: Path, start: float, dur: float, fps: float, ref: list[int], crop: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = crop
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(video), "-t", f"{dur:.3f}",
        "-vf", f"fps={fps},scale={ref[0]}:{ref[1]},crop={w}:{h}:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"frame extraction failed: {proc.stderr[-300:]!r}")
    return np.frombuffer(proc.stdout, np.uint8).reshape(-1, h, w, 3).astype(np.int16)


def detect_kill_credit(video: Path, use_time: float, slots: list[dict], cfg: dict) -> dict:
    """Did the player take part in a kill soon after a skill (e.g. anti-heal grenade -> enemy tank dies)?

    The game shows a red "skull + name" banner just below the screen centre for the player's own
    eliminations and assists. A banner that newly appears within window_seconds after the use (one
    already on screen at the use belongs to an earlier kill) gives the kill time. The enemy is the team-
    list slot whose "%" box loses its colour most at that moment, relative to the other enemy slots
    (a dead player's box turns dull; the other slots cancel out background changes behind the list).
    cfg: hud_check.kill_banner.
    """
    fps, ref = float(cfg["fps"]), cfg["reference_resolution"]
    b = cfg["banner"]
    lead = 0.5
    fr = _frames(video, use_time - lead, cfg["window_seconds"] + lead, fps, ref, (b["x"], b["y"], b["w"], b["h"]))
    r, g, bl = fr[..., 0], fr[..., 1], fr[..., 2]
    on = ((r > 150) & (r - g > 90) & (r - bl > 40)).mean(axis=(1, 2)) >= b["min_ratio"]
    times = use_time - lead + np.arange(len(on)) / fps
    need = max(1, int(round(b["min_seconds"] * fps)))
    kill_t = None
    for i in range(1, len(on)):
        # a banner that starts here (off just before), after the use, and stays for min_seconds
        if times[i] > use_time and on[i] and not on[i - 1] and on[i:i + need].all() and len(on[i:i + need]) == need:
            kill_t = float(times[i])
            break
    if kill_t is None:
        return {"status": "none", "note": f"사용 후 {cfg['window_seconds']:g}초 안에 새 처치 표시 없음"}
    out = {"status": "kill", "time_seconds": round(kill_t, 2), "seconds_after": round(kill_t - use_time, 2)}
    if len(slots) >= 2:
        d = cfg["slot_box"]  # banners also announce sleeps, saves and assists: only a fading enemy slot confirms a kill
        xs = [s["x"] for s in slots]
        x_lo, x_hi = min(xs) + d["dx0"], max(xs) + d["dx1"]
        crop = (x_lo, d["y"], x_hi - x_lo, d["h"])

        def colour(start: float, dur: float) -> np.ndarray:
            f = _frames(video, start, dur, fps, ref, crop).astype(np.float32) / 255
            mx, mn = f.max(axis=3), f.min(axis=3)
            sv = (mx - mn) / (mx + 1e-6) * mx                       # saturation x value
            per = np.array([[x[:, s["x"] + d["dx0"] - x_lo:s["x"] + d["dx1"] - x_lo].mean() for s in slots] for x in sv])
            return np.median(per / np.median(per, axis=1, keepdims=True), axis=0)   # relative to the other slots

        drop = 1 - colour(kill_t, d["after_seconds"]) / np.maximum(colour(use_time - 1.0, 1.0), 1e-6)
        i = int(np.argmax(drop))
        out["slot_fade"] = round(float(drop[i]), 2)
        if drop[i] >= d["min_drop"]:
            s = slots[i]
            out.update(slot=s["slot"], player_name=s["player_name"], hero=s.get("hero"))
    if "player_name" not in out:
        return {"status": "none", "note": f"사용 {out['seconds_after']:g}초 뒤 화면 알림은 있으나 적 칸 변화 없음(재움·구원 알림 등): 처치 미확인"}
    out["note"] = f"사용 {out['seconds_after']:g}초 뒤 {out['player_name']}({out.get('hero') or '?'}) 처치 관여 (화면 처치 알림 + 팀 목록 칸 변화)"
    return out


def detect_antiheal_targets(video: Path, use_time: float, slots: list[dict], cfg: dict) -> dict:
    """Who got the anti-heal of the player's grenade, read from the team list at the top.

    A player under anti-heal has the health bar under their name in the team list outlined in magenta
    (measured: 0 before, ~0.27 of the bar area while it lasts). A slot counts when the outline starts
    within onset_before / onset_after of the HUD use time and stays min_seconds. Enemy slots are the
    targets; ally slots outlined at the same moment point to a grenade that came back (deflect).
    cfg: hud_check.antiheal_bars. slots: every team-list slot with x and side ("ally"/"enemy").
    """
    fps, ref = float(cfg["fps"]), cfg["reference_resolution"]
    start = max(0.0, use_time - cfg["onset_before_seconds"])
    span = cfg["onset_before_seconds"] + cfg["onset_after_seconds"] + cfg["min_seconds"] + 0.5
    fr = _frames(video, start, span, fps, ref, (0, cfg["y"], ref[0], cfg["h"]))
    if len(fr) == 0 or not slots:
        return {"status": "unverifiable", "enemies": [], "allies": [], "note": "판별용 프레임 부족"}
    r, g, b = fr[..., 0], fr[..., 1], fr[..., 2]
    mag = (r > 140) & (b > 140) & (g < np.minimum(r, b) - 60) & (np.abs(r - b) < 80)
    times = start + np.arange(len(fr)) / fps
    need = max(1, int(round(cfg["min_seconds"] * fps)))
    hit: dict[str, list[dict]] = {"enemy": [], "ally": []}
    for s in slots:
        on = mag[:, :, s["x"] + cfg["dx0"]:s["x"] + cfg["dx1"]].mean(axis=(1, 2)) >= cfg["min_ratio"]
        if on[times < use_time - cfg["onset_before_seconds"] + 0.01].any():
            continue  # already outlined before the throw: someone else's anti-heal
        for i in range(len(on) - need + 1):
            if times[i] > use_time + cfg["onset_after_seconds"]:
                break
            if on[i:i + need].all():
                hit[s["side"]].append({"slot": s["slot"], "player_name": s["player_name"], "hero": s.get("hero"),
                                       "onset_seconds": round(float(times[i]), 2)})
                break
    names = lambda xs: ", ".join(f"{x['player_name']}({x.get('hero') or '?'})" for x in xs)
    note = f"치유 차단 적 {len(hit['enemy'])}명" + (f": {names(hit['enemy'])}" if hit["enemy"] else "")
    if hit["ally"]:
        note += f"; 아군 {len(hit['ally'])}명도 치유 차단({names(hit['ally'])}): 반사 가능성"
    return {"status": "hit" if hit["enemy"] else "none", "enemies": hit["enemy"], "allies": hit["ally"],
            "note": note + " (팀 목록 체력바 보라 테두리)"}


def _outlined_bars(mask: np.ndarray, min_len: int, gap: tuple[int, int], max_len: int = 10_000) -> list[tuple[int, int]]:
    """(y, x) of overhead health bars drawn with a coloured outline: a closed box whose top and bottom
    are horizontal runs of `mask` (min_len..max_len long, gap rows apart, overlapping) and whose left
    and right ends are mostly `mask` too. Loose patches of the colour (effects, text) do not close a box."""
    rows: dict[int, list[tuple[int, int]]] = {}
    for y in np.nonzero(mask.sum(axis=1) >= min_len)[0]:
        xs = np.nonzero(mask[y])[0]
        runs, s0, p = [], xs[0], xs[0]
        for x in xs[1:]:
            if x > p + 2:
                if min_len <= p - s0 + 1 <= max_len:
                    runs.append((s0, p))
                s0 = x
            p = x
        if min_len <= p - s0 + 1 <= max_len:
            runs.append((s0, p))
        if runs:
            rows[int(y)] = runs
    bars: list[tuple[int, int]] = []
    for y, rs in rows.items():
        for a, b in rs:
            for dy in range(gap[0], gap[1] + 1):
                for c, d in rows.get(y + dy, []):
                    lo, hi = max(a, c), min(b, d)
                    if hi - lo < 0.8 * max(b - a, d - c):
                        continue
                    inner = mask[y + 1:y + dy]
                    left = inner[:, max(0, lo - 2):lo + 3].any(axis=1).mean()
                    right = inner[:, max(0, hi - 2):hi + 3].any(axis=1).mean()
                    fill = inner[:, lo + 3:hi - 2].mean() if hi - lo > 6 else 1.0   # a bar holds health cells, not the colour
                    if left >= 0.6 and right >= 0.6 and fill <= 0.2 and not any(abs(y - yy) <= gap[1] + 2 and abs(a - aa) < 20 for yy, aa in bars):
                        bars.append((y, int(a)))
    return bars


def detect_heal_boost(video: Path, use_time: float, cfg: dict) -> dict:
    """Allies caught by the player's grenade: a healed ally gets a yellow-outlined overhead health bar
    with a yellow "+" (user tip: yellow light = ally), and the player's own HUD icon left of the health
    number turns yellow when the player is in the splash. Counts are the most bars seen in one frame."""
    fps, ref = float(cfg["fps"]), cfg["reference_resolution"]
    w = cfg["world"]
    fr = _frames(video, use_time - cfg["onset_before_seconds"], cfg["onset_before_seconds"] + cfg["onset_after_seconds"],
                 fps, ref, (0, w["y0"], ref[0], w["y1"] - w["y0"]))
    if len(fr) == 0:
        return {"status": "unverifiable", "allies_on_screen": 0, "self": False, "note": "판별용 프레임 부족"}
    r, g, b = fr[..., 0], fr[..., 1], fr[..., 2]
    yel = (r > 180) & (g > 160) & (b < 130) & (r - b > 80)
    # baseline from the first base_seconds only: the HUD use time can lag the throw by ~0.6 s
    nb = max(1, int(cfg["base_seconds"] * fps))
    per = [_outlined_bars(m, w["min_len"], tuple(w["gap"]), w["max_len"]) for m in yel]
    # a bar is an overhead health bar only if it stays (about the same place) for persist_frames frames;
    # the grenade burst itself makes box-like yellow shapes for a frame or two
    k = int(w["persist_frames"])
    near = lambda p, q: abs(p[0] - q[0]) <= w["track_px"] and abs(p[1] - q[1]) <= w["track_px"]
    tracks: list[tuple[int, int, int]] = []          # (y, x, first frame) of persistent bars
    for i in range(len(per) - k + 1):
        for p0 in per[i]:
            chain, cur = True, p0
            for j in range(1, k):
                nxt = next((q for q in per[i + j] if near(cur, q)), None)
                if nxt is None:
                    chain = False
                    break
                cur = nxt
            if chain and not any(near(p0, (ty, tx)) for ty, tx, _ in tracks):
                tracks.append((p0[0], p0[1], i))
    base = sum(1 for _, _, i in tracks if i < nb)
    allies = sum(1 for _, _, i in tracks if i >= nb) if base == 0 else max(0, sum(1 for _, _, i in tracks if i >= nb) - base)
    ic = cfg["self_icon"]
    own = _frames(video, use_time - cfg["onset_before_seconds"], cfg["onset_before_seconds"] + cfg["onset_after_seconds"],
                  fps, ref, (ic["x0"], ic["y0"], ic["x1"] - ic["x0"], ic["y1"] - ic["y0"]))
    orr, og, ob = own[..., 0], own[..., 1], own[..., 2]
    oy = ((orr > 180) & (og > 150) & (ob < 110)).mean(axis=(1, 2)) >= ic["min_ratio"]
    need = max(1, int(round(ic["min_seconds"] * fps)))
    me = any(oy[i:i + need].all() for i in range(len(oy) - need + 1))
    parts = ([f"화면 속 아군 {allies}명"] if allies else []) + (["자신"] if me else [])
    return {"status": "hit" if parts else "none", "allies_on_screen": allies, "self": me,
            "note": ("치유 강화(노란 표시): " + ", ".join(parts)) if parts else "노란 치유 강화 표시 없음: 아군에게 맞지 않음"}


def team_health_timeline(video: Path, duration: float, xs: list[int], cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Health of every team-list slot over the whole video, from the segmented bar under each name.

    Fill = how far the filled cells reach across the bar (white health, blue shields, orange armour all
    count; empty cells are grey), as a share of the bar width. Counting white area instead under-read
    a Sigma with shields as 36% when the bar showed about 70% (user, video 1 1:07). A slot with no white
    health cell (a dead player's X) or with bright rows just above the bar (effects) is -1 (unknown).
    Measured: BERNAR 0.56 with 131/250 HP and 0.16 with 28/250 (video 2).
    Returns (times, fills[len(times), len(xs)]), fills 0..1 or -1.
    """
    fps, ref = float(cfg["fps"]), cfg["reference_resolution"]
    top = cfg["y"] - cfg["above_h"]
    fr = _frames(video, 0.0, duration, fps, ref, (0, top, ref[0], cfg["above_h"] + cfg["h"]))
    r, g, b = fr[..., 0], fr[..., 1], fr[..., 2]
    white = fr.min(axis=3) > cfg["white_threshold"]
    bright = white | ((b > 160) & (b - r > 40)) | ((r > 200) & (g > 120) & (b < 90))
    above, bar = bright[:, :cfg["above_h"]], bright[:, cfg["above_h"]:]
    bar_white = white[:, cfg["above_h"]:]
    width = cfg["dx1"] - cfg["dx0"]
    out = np.full((len(fr), len(xs)), -1.0)
    for j, x in enumerate(xs):
        cols = bar[:, :, x + cfg["dx0"]:x + cfg["dx1"]].mean(axis=1) >= cfg["min_rows"]          # (frames, width)
        noise = above[:, :, x + cfg["dx0"]:x + cfg["dx1"]].mean(axis=(1, 2)) > cfg["max_above"]
        last = np.where(cols.any(axis=1), width - 1 - np.argmax(cols[:, ::-1], axis=1), -1)
        # a living player's bar always has white health cells; none (only a dead player's X, effects) = unknown
        alive = (bar_white[:, :, x + cfg["dx0"]:x + cfg["dx1"]].mean(axis=1) >= cfg["min_rows"]).sum(axis=1) >= cfg["min_white_cols"]
        out[:, j] = np.where(noise | ~alive, -1.0, (last + 1) / width)
    return np.arange(len(fr)) / fps, out


def _box_sum(m: np.ndarray, r: int) -> np.ndarray:
    c = np.pad(m.astype(np.int32), ((r + 1, r), (r + 1, r))).cumsum(0).cumsum(1)
    return c[2 * r + 1:, 2 * r + 1:] - c[:-2 * r - 1, 2 * r + 1:] - c[2 * r + 1:, :-2 * r - 1] + c[:-2 * r - 1, :-2 * r - 1]


def _cell_clusters(cells: np.ndarray, min_size: int) -> list[tuple[int, float]]:
    """(size, centre x as a share of the grid width) of 8-connected groups of occupied grid cells
    (a group ~ one character's outline)."""
    seen = np.zeros_like(cells, dtype=bool)
    h, w = cells.shape
    out = []
    for i in range(h):
        for j in range(w):
            if cells[i, j] and not seen[i, j]:
                stack, n, xs = [(i, j)], 0, 0
                seen[i, j] = True
                while stack:
                    a, b = stack.pop()
                    n += 1
                    xs += b
                    for da in (-1, 0, 1):
                        for db in (-1, 0, 1):
                            x, y = a + da, b + db
                            if 0 <= x < h and 0 <= y < w and cells[x, y] and not seen[x, y]:
                                seen[x, y] = True
                                stack.append((x, y))
                if n >= min_size:
                    out.append((n, (xs / n + 0.5) / w))
    return out


def screen_presence(video: Path, start: float, end: float, cfg: dict, ally_red: bool = True) -> list[dict]:
    """Allies and enemies on screen, from their coloured outlines, every 1/fps seconds.

    In these videos allies are outlined in the ally colour (red/pink) and enemies in cyan. Only thin
    lines count (filled patches such as a red bus or the hit vignette do not), only inside the world view
    (team list, HUD, screen edges and kill feed excluded), and each 8-connected group of grid cells is
    one character. Measured: video 2 1:01-1:11 before a death, 0 allies and 1-3 enemies on screen;
    video 1 3:03-3:08 (coach: back to the team while moving) 0 allies, 1-5 enemies.
    Returns [{time_seconds, allies, enemies, enemy_largest, ally_x}] (enemy_largest in cells: big = close;
    ally_x: centre of each ally group across the world view, 0 = left edge, 1 = right edge).
    """
    fps, (rw, rh) = float(cfg["fps"]), cfg["reference_resolution"]
    x0, y0, x1, y1 = cfg["world"]
    fr = _frames(video, start, max(0.0, end - start), fps, [rw, rh], (x0, y0, x1 - x0, y1 - y0))
    kx0, ky1 = cfg["kill_feed"]
    cell, thin_max = int(cfg["cell"]), int(cfg["thin_max"])
    out = []
    for i, f in enumerate(fr):
        r, g, b = f[..., 0], f[..., 1], f[..., 2]
        red = (r > 180) & (g < 90) & (b > 40) & (b < 170) & (r - g > 110)
        cyan = (g > 160) & (b > 160) & (r < 130)
        ally, enemy = (red, cyan) if ally_red else (cyan, red)
        counts = []
        for m in (ally, enemy):
            m = m.copy()
            m[:ky1 - y0, kx0 - x0:] = False                      # kill feed
            thin = m & (_box_sum(m, 2) <= thin_max)              # outlines are thin lines
            h, w = thin.shape
            cells = thin[:h // cell * cell, :w // cell * cell].reshape(h // cell, cell, w // cell, cell).sum(axis=(1, 3)) >= 3
            counts.append(_cell_clusters(cells, int(cfg["min_cluster"])))
        out.append({"time_seconds": round(start + i / fps, 2), "allies": len(counts[0]), "enemies": len(counts[1]),
                    "enemy_largest": max((n for n, _ in counts[1]), default=0),
                    "ally_x": [round(x, 3) for _, x in counts[0]]})
    return out


def camera_turn(video: Path, start: float, end: float, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Horizontal camera turn, accumulated from start, in screen widths (+ = turned right), every 1/fps s.

    Each frame's upper world view (far scenery; the gun, HUD and team list left out) is matched to the
    previous one by phase correlation: the shift that best lines them up is how far the view panned.
    A screen width is the field of view, whatever the player's FOV setting is, so no angle is claimed.
    It needs a high frame rate: a quick 180-degree flick moves more than half a screen between frames at
    4 fps and the match fails (measured: video 1 3:05 turn lost at 4 fps, ~1.3 widths at 30 fps).
    Measured: video 1 3:05 the player turns from the team toward the payload (~1.3 widths) and back at
    3:10. Drifts over long spans (walking adds small shifts), so compare only nearby times.
    cfg: hud_check.screen_presence.turn. Returns (times, turn).
    """
    fps, (rw, rh) = float(cfg["fps"]), cfg["reference_resolution"]
    x, y, w, h = cfg["region"]
    dur = max(0.0, end - start)
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(video), "-t", f"{dur:.3f}",
        "-vf", f"fps={fps},scale={rw}:{rh},crop={w}:{h}:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ], capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"frame extraction failed: {proc.stderr[-300:]!r}")
    fr = np.frombuffer(proc.stdout, np.uint8).reshape(-1, h, w)
    if not len(fr):
        return np.zeros(0), np.zeros(0)
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    turn, prev = [0.0], None
    for f in fr:
        spec = np.fft.rfft2((f - f.mean()) * win)
        if prev is not None:
            r = spec * np.conj(prev)
            corr = np.fft.irfft2(r / (np.abs(r) + 1e-6), s=(h, w))
            dx = int(np.unravel_index(np.argmax(corr), corr.shape)[1])
            dx = dx if dx < w // 2 else dx - w
            turn.append(turn[-1] - dx / rw)      # scenery moving right = the view turned left
        prev = spec
    return start + np.arange(len(turn)) / fps, np.array(turn)

