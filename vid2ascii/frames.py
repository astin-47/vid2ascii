"""Frame geometry (pure math), ffprobe probing and ffmpeg frame decoding."""
import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from . import FFmpegNotFound, InputError

# --- Pure layout math -------------------------------------------------------

ASPECTS = {"reel": 9 / 16, "square": 1.0, "wide": 16 / 9, "cinematic": 2.39}


@dataclass(frozen=True)
class Layout:
    cols: int
    rows: int
    cw: int
    ch: int
    out_w: int
    out_h: int


def _even_down(x: int) -> int:
    return x - (x % 2)


def parse_aspect(spec: str | None, src_w: int, src_h: int) -> float:
    if spec is None or spec == "source":
        return src_w / src_h
    if spec in ASPECTS:
        return ASPECTS[spec]
    parts = spec.lower().split("x")
    if len(parts) == 2 and all(p.isascii() and p.isdigit() for p in parts):
        w, h = int(parts[0]), int(parts[1])
        if w > 0 and h > 0:
            return w / h
    raise ValueError(f"invalid aspect: {spec!r}")


def compute_layout(aspect: float, cols: int, font_w: float, font_h: float,
                   long_side: int = 1920) -> Layout:
    if aspect <= 0:
        raise ValueError("aspect must be positive")
    if cols < 1:
        raise ValueError("cols must be at least 1")
    if aspect >= 1:
        w, h = long_side, long_side / aspect
    else:
        w, h = long_side * aspect, long_side
    cw = max(4, round(w / cols))
    if cols % 2 and cw % 2:
        cw += 1  # keep cols*cw even
    ch = max(4, round(cw * font_h / font_w))
    real_w = cols * cw
    rows = max(1, round(real_w / aspect / ch))
    if (rows * ch) % 2:
        ch += 1  # keep rows*ch even
        rows = max(1, round(real_w / aspect / ch))
    return Layout(cols, rows, cw, ch, real_w, rows * ch)


def crop_filter(src_w: int, src_h: int, aspect: float) -> str:
    src_aspect = src_w / src_h
    if abs(src_aspect - aspect) / aspect < 0.01:
        return ""
    if src_aspect > aspect:
        return f"crop={_even_down(round(src_h * aspect))}:{src_h}"
    return f"crop={src_w}:{_even_down(round(src_w / aspect))}"


# --- ffprobe / ffmpeg decoding ----------------------------------------------


def require_ffmpeg() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise FFmpegNotFound(
            f"{' and '.join(missing)} not found on PATH. Install ffmpeg: "
            "Windows: winget install ffmpeg; macOS: brew install ffmpeg; "
            "Linux: apt install ffmpeg")


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool


def _parse_rate(text) -> float:
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den) if den else float(num)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return value if value > 0 else 0.0


def _parse_duration(text) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def probe(path) -> VideoInfo:
    err = InputError(f"cannot read video: {path}")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", "-protocol_whitelist", "file,pipe", str(path)],
            capture_output=True, check=False)
    except OSError as exc:
        raise err from exc
    if result.returncode != 0:
        raise err
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise err from exc
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise err
    width, height = video.get("width") or 0, video.get("height") or 0
    if width <= 0 or height <= 0:
        raise err
    fps = (_parse_rate(video.get("avg_frame_rate"))
           or _parse_rate(video.get("r_frame_rate")))
    duration = (_parse_duration(data.get("format", {}).get("duration"))
                or _parse_duration(video.get("duration")))
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return VideoInfo(int(width), int(height), fps, duration, has_audio)


def _grid_filters(info: VideoInfo, layout: Layout, aspect: float) -> list[str]:
    """Center-crop to the output aspect, then area-downscale to the character grid."""
    filters = []
    crop = crop_filter(info.width, info.height, aspect)
    if crop:
        filters.append(crop)
    return filters + [f"scale={layout.cols}:{layout.rows}:flags=area", "format=rgb24"]


def iter_frames(path, info: VideoInfo, start: float, duration: float,
                fps: float, layout: Layout, aspect: float) -> Iterator[np.ndarray]:
    filters = [f"fps={fps}", *_grid_filters(info, layout, aspect)]
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", str(start),
           "-protocol_whitelist", "file,pipe", "-i", str(path), "-t", str(duration), "-an",
           "-vf", ",".join(filters), "-f", "rawvideo", "-pix_fmt", "rgb24",
           "pipe:1"]
    frame_bytes = layout.rows * layout.cols * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            bufsize=frame_bytes * 4)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(
                layout.rows, layout.cols, 3)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def decode_frame(path, info: VideoInfo, t: float, layout: Layout,
                 aspect: float) -> np.ndarray:
    """Decode the single frame at time t, already cropped and scaled to the grid."""
    frame_bytes = layout.rows * layout.cols * 3
    vf = ",".join(_grid_filters(info, layout, aspect))
    for when in (t, max(0.0, info.duration - 0.1)):
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, when):.3f}",
               "-protocol_whitelist", "file,pipe", "-i", str(path), "-an",
               "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0 and len(result.stdout) >= frame_bytes:
            return np.frombuffer(result.stdout[:frame_bytes], dtype=np.uint8).reshape(
                layout.rows, layout.cols, 3)
    raise InputError(f"cannot decode a frame at {t:.2f}s from {path}")
