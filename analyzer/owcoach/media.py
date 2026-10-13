"""FFmpeg/ffprobe helpers: probing, input validation, proxy encoding, clipping, frame extraction."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MediaError(Exception):
    pass


class InputRejected(Exception):
    """The video does not meet the input conditions. Message is user-facing (Korean)."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass
class ProbeInfo:
    duration_seconds: float
    width: int
    height: int
    fps: float | None
    codec: str | None
    has_audio: bool
    size_bytes: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise MediaError(f"{tool} not found on PATH")
    return path


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-2000:]}")
    return proc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    if "/" in rate:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else None
    return float(rate)


def probe(path: Path) -> ProbeInfo:
    proc = _run([
        _require("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise InputRejected(["영상 스트림이 없습니다."])
    duration = video.get("duration") or data.get("format", {}).get("duration")
    if duration is None:
        raise InputRejected(["영상 길이를 확인할 수 없습니다."])
    return ProbeInfo(
        duration_seconds=round(float(duration), 3),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=_parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
        codec=video.get("codec_name"),
        has_audio=any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        size_bytes=path.stat().st_size,
    )


def validate_input(path: Path, info: ProbeInfo, rules: dict) -> list[str]:
    """Returns a list of Korean rejection reasons; empty list means the video is accepted."""
    reasons = []
    if path.suffix.lower() not in rules["allowed_extensions"]:
        reasons.append(f"지원하지 않는 파일 형식입니다: {path.suffix} (지원: {', '.join(rules['allowed_extensions'])})")
    if info.duration_seconds > rules["max_duration_seconds"]:
        reasons.append(f"영상 길이 {info.duration_seconds:.1f}초가 최대 {rules['max_duration_seconds']}초를 넘습니다.")
    if info.duration_seconds <= 0:
        reasons.append("영상 길이가 0초입니다.")
    # Portrait/rotated recordings are compared on their long/short edge.
    long_edge, short_edge = max(info.width, info.height), min(info.width, info.height)
    if long_edge < rules["min_width"] or short_edge < rules["min_height"]:
        reasons.append(
            f"해상도 {info.width}x{info.height}가 최소 {rules['min_width']}x{rules['min_height']}보다 낮아 HUD 판독이 어렵습니다."
        )
    if info.size_bytes > rules["max_file_size_mb"] * 1024 * 1024:
        reasons.append(f"파일 크기가 {rules['max_file_size_mb']}MB를 넘습니다.")
    return reasons


def make_proxy(src: Path, dst: Path, max_height: int, crf: int, fps: int) -> Path:
    """Re-encode without trimming so that proxy timestamps equal original timestamps."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.mp4")
    _run([
        _require("ffmpeg"), "-y", "-v", "error", "-i", str(src),
        "-vf", f"scale=-2:'min({max_height},ih)',fps={fps}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-an", "-movflags", "+faststart", str(tmp),
    ])
    tmp.replace(dst)
    return dst


def cut_clip(src: Path, dst: Path, start: float, end: float, max_height: int, crf: int) -> Path:
    """Frame-accurate cut (re-encoded). Clip time 0 corresponds to original time `start`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.mp4")
    _run([
        _require("ffmpeg"), "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{end - start:.3f}",
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-an", "-movflags", "+faststart", str(tmp),
    ])
    tmp.replace(dst)
    return dst


def extract_crop_png(src: Path, timestamp: float, ref_size: tuple[int, int], region: dict, upscale: int) -> bytes:
    """One frame at `timestamp`, scaled to ref_size, cropped to region {x,y,w,h} and upscaled. PNG bytes."""
    w, h = ref_size
    proc = subprocess.run([
        _require("ffmpeg"), "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(src), "-frames:v", "1",
        "-vf", f"scale={w}:{h},crop={region['w']}:{region['h']}:{region['x']}:{region['y']},"
               f"scale=iw*{upscale}:ih*{upscale}:flags=lanczos",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ], capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise MediaError(f"crop extraction failed at {timestamp}s: {proc.stderr[-500:]!r}")
    return proc.stdout


def extract_frame(src: Path, timestamp: float, dst: Path, max_width: int) -> Path | None:
    """Extract one frame at an original-video timestamp. Returns None if no frame could be decoded."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([
        _require("ffmpeg"), "-y", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(dst),
    ], capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return None
    return dst
