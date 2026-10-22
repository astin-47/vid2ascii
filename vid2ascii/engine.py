"""Conversion engine shared by the CLI and the web server: decode -> render -> encode."""

import math
import os
import subprocess
import sys
import threading
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import Cancelled, InputError, Vid2AsciiError
from .atlas import build_atlas, find_font, font_cell_ratio, resolve_charset
from .frames import (Layout, VideoInfo, compute_layout, decode_frame, iter_frames,
                     parse_aspect, probe)
from .render import AutoContrast, Renderer

MAX_DURATION = 60.0
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
COLORS = ("original", "green", "amber", "mono")
# GIF export sizes: (width in px, or None to keep the video size; frames per second)
GIF_SIZES = {"small": (480, 12), "medium": (720, 15), "full": (None, 15)}

Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class Options:
    start: float = 0.0
    duration: float | None = None
    fps: float = 24.0
    cols: int = 120
    color: str = "original"
    charset: str = "standard"
    contrast: str = "auto"
    beat: bool = False
    aspect: str | None = None
    font: str | None = None

    def validate(self) -> None:
        if not self.fps > 0:
            raise ValueError("fps must be > 0")
        if self.cols < 1:
            raise ValueError("cols must be >= 1")
        if self.start < 0:
            raise ValueError("start must be >= 0")
        if self.duration is not None and not self.duration > 0:
            raise ValueError("duration must be > 0")
        if self.color not in COLORS:
            raise ValueError(f"invalid color: {self.color!r}")
        resolve_charset(self.charset)
        AutoContrast(self.contrast)

    @classmethod
    def from_dict(cls, data: dict) -> "Options":
        casts = {"start": float, "duration": float, "fps": float, "cols": int,
                 "beat": bool, "color": str, "charset": str, "contrast": str,
                 "aspect": str, "font": str}
        kwargs = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if value is None or value == "":
                if f.default is None:
                    kwargs[f.name] = None
                continue
            try:
                kwargs[f.name] = casts[f.name](value)
            except (TypeError, ValueError):
                raise ValueError(f"invalid {f.name}: {value!r}") from None
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConvertResult:
    path: Path
    width: int
    height: int
    duration: float
    frames: int


@dataclass
class Plan:
    info: VideoInfo
    start: float
    dur: float
    fps: float
    n_frames: int
    layout: Layout
    chars: str
    font: str


def plan(src: Path, opts: Options, long_side: int = 1920,
         info: VideoInfo | None = None) -> Plan:
    """Probe (unless info is given), clamp the time range and lay out the grid."""
    src = Path(src)
    if not src.is_file():
        raise InputError(f"input not found: {src}")
    info = info or probe(src)
    start = opts.start
    if info.duration > 0:
        dur = min(MAX_DURATION, opts.duration or math.inf, info.duration - start)
        if dur <= 0:
            raise InputError("--start is past the end of the video")
    else:  # duration unknown (e.g. a piped WebM): the decoder stops at EOF
        dur = min(MAX_DURATION, opts.duration or MAX_DURATION)
    chars = resolve_charset(opts.charset)
    aspect = parse_aspect(opts.aspect, info.width, info.height)
    font = find_font(opts.font)
    layout = compute_layout(aspect, opts.cols, *font_cell_ratio(font), long_side=long_side)
    n_frames = max(1, round(dur * opts.fps))
    return Plan(info, start, dur, opts.fps, n_frames, layout, chars, font)


def _beat_envelope(src: Path, p: Plan):
    if not p.info.has_audio:
        print(f"warning: {src.name} has no audio; --beat ignored", file=sys.stderr)
        return None
    from .audio import beat_envelope, detect_beats, load_audio

    sr = 22050
    samples = load_audio(src, p.start, p.dur, sr=sr)
    return beat_envelope(detect_beats(samples, sr), p.n_frames, p.fps)


def _setup(src: Path, opts: Options):
    p = plan(src, opts)
    renderer = Renderer(build_atlas(p.chars, p.font, p.layout.cw, p.layout.ch),
                        color=opts.color, contrast=opts.contrast)
    envelope = _beat_envelope(src, p) if opts.beat else None
    frames = iter_frames(src, p.info, p.start, p.dur, p.fps, p.layout,
                         p.layout.out_w / p.layout.out_h)
    return p, renderer, envelope, frames


def _render_frames(frames, renderer, envelope, n_frames):
    """Yield rendered frames for at most n_frames decoded frames."""
    for i, cells in enumerate(frames):
        if i >= n_frames:
            break
        strength = float(envelope[i]) if envelope is not None else 0.0
        yield renderer.render(cells, strength)


def _encode(rendered, dst: Path, src: Path, p: Plan, on_progress: Progress | None,
            cancel: threading.Event | None) -> int:
    """Encode into a .partial sibling and move it into place only on success."""
    partial = dst.with_name(dst.stem + ".partial" + dst.suffix)
    layout = p.layout
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{layout.out_w}x{layout.out_h}", "-r", str(p.fps), "-i", "-"]
    if p.info.has_audio:
        cmd += ["-ss", str(p.start), "-t", str(p.dur), "-protocol_whitelist", "file,pipe",
                "-i", str(src),
                "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-shortest", "-movflags", "+faststart", str(partial)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    tail = deque(maxlen=15)

    def drain():
        for line in proc.stderr:
            tail.append(line.decode(errors="replace").rstrip())

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    count = 0
    broken = False
    try:
        try:
            for frame in rendered:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                try:
                    proc.stdin.write(frame.data)
                except OSError:  # BrokenPipeError, or EINVAL on Windows
                    broken = True
                    break
                count += 1
                if on_progress is not None:
                    on_progress(count, p.n_frames)
        except BaseException:
            proc.kill()  # abort: do not let ffmpeg finalize a truncated file
            raise
        finally:
            try:
                proc.stdin.close()
            except OSError:
                broken = True
            if count == 0 and proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join()
        details = "\n".join(tail)
        if count == 0 and not broken:
            raise Vid2AsciiError(f"ffmpeg produced no frames for {src}")
        if broken or proc.returncode != 0:
            raise Vid2AsciiError(f"ffmpeg encoding failed for {dst}:\n{details}")
        os.replace(partial, dst)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return count


def convert(src: Path, dst: Path, opts: Options, *, overwrite: bool = False,
            on_progress: Progress | None = None,
            cancel: threading.Event | None = None) -> ConvertResult:
    src, dst = Path(src), Path(dst)
    if dst.exists() and not overwrite:
        raise InputError(f"output exists: {dst} (use --overwrite)")
    opts.validate()
    p, renderer, envelope, frames = _setup(src, opts)
    dst.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_frames(frames, renderer, envelope, p.n_frames)
    try:
        count = _encode(rendered, dst, src, p, on_progress, cancel)
    finally:
        rendered.close()
        frames.close()
    return ConvertResult(dst, p.layout.out_w, p.layout.out_h, count / p.fps, count)


def write_previews(src: Path, out_base: Path, opts: Options, count: int) -> list[Path]:
    """Render only `count` evenly spaced frames and save them as PNGs next to out_base."""
    src, out_base = Path(src), Path(out_base)
    opts.validate()
    p, renderer, envelope, frames = _setup(src, opts)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    indices = np.round(np.linspace(0, p.n_frames - 1, count)).astype(int)
    written = []
    try:
        for i, cells in enumerate(frames):
            targets = np.flatnonzero(indices == i)
            if targets.size:
                strength = float(envelope[i]) if envelope is not None else 0.0
                frame = renderer.render(cells, strength)
                for j in targets:
                    path = out_base.parent / f"{out_base.stem}_preview_{j + 1:02d}.png"
                    Image.fromarray(frame).save(path)
                    written.append(path)
            if i >= indices[-1]:
                break
    finally:
        frames.close()
    if not written:
        raise Vid2AsciiError(f"ffmpeg produced no frames for {src}")
    return written


class _LRU:
    def __init__(self, size: int):
        self.size = size
        self.items: OrderedDict = OrderedDict()

    def get(self, key, make):
        if key in self.items:
            self.items.move_to_end(key)
            return self.items[key]
        value = make()
        self.items[key] = value
        if len(self.items) > self.size:
            self.items.popitem(last=False)
        return value


class PreviewCache:
    """Thread-safe caches for probe results, decoded grid frames and glyph atlases."""

    def __init__(self, max_frames: int = 64, max_atlases: int = 16):
        self._lock = threading.Lock()
        self._infos = _LRU(64)
        self._frames = _LRU(max_frames)
        self._atlases = _LRU(max_atlases)

    @staticmethod
    def _mtime(src: Path) -> int:
        return Path(src).stat().st_mtime_ns

    def _cached(self, lru: _LRU, key, make):
        with self._lock:
            if key in lru.items:
                return lru.get(key, make)
        value = make()  # slow work runs outside the lock
        with self._lock:
            return lru.get(key, lambda: value)

    def info(self, src: Path) -> VideoInfo:
        return self._cached(self._infos, (str(src), self._mtime(src)), lambda: probe(src))

    def frame(self, src: Path, info: VideoInfo, t: float, fps: float, layout: Layout,
              aspect: float) -> np.ndarray:
        key = (str(src), self._mtime(src), round(t * fps), layout.cols, layout.rows,
               round(aspect, 4))
        return self._cached(self._frames, key,
                            lambda: decode_frame(src, info, t, layout, aspect))

    def atlas(self, font: str, chars: str, cw: int, ch: int) -> np.ndarray:
        return self._cached(self._atlases, (font, chars, cw, ch),
                            lambda: build_atlas(chars, font, cw, ch))


_default_cache = PreviewCache()


def render_still(src: Path, t: float, opts: Options, *, long_side: int = 1920,
                 cache: PreviewCache | None = None) -> np.ndarray:
    """Render the single frame at time t (no beat effect) as uint8 (H, W, 3)."""
    src = Path(src)
    opts.validate()
    cache = cache or _default_cache
    if not src.is_file():
        raise InputError(f"input not found: {src}")
    info = cache.info(src)
    p = plan(src, opts, long_side=long_side, info=info)
    t = min(max(0.0, t), info.duration) if info.duration > 0 else max(0.0, t)
    layout = p.layout
    cells = cache.frame(src, info, t, p.fps, layout, layout.out_w / layout.out_h)
    atlas = cache.atlas(p.font, p.chars, layout.cw, layout.ch)
    return Renderer(atlas, color=opts.color, contrast=opts.contrast).render(cells)


def layout_for(src: Path, opts: Options, *, long_side: int = 1920,
               cache: PreviewCache | None = None) -> Layout:
    """The character grid and output size a render with these options would use."""
    cache = cache or _default_cache
    return plan(Path(src), opts, long_side=long_side, info=cache.info(Path(src))).layout


def to_gif(src: Path, dst: Path, size: str = "small", *, on_progress: Progress | None = None,
           cancel: threading.Event | None = None) -> ConvertResult:
    """Convert a finished video to an animated GIF with a palette made for its colours."""
    if size not in GIF_SIZES:
        raise ValueError(f"invalid GIF size: {size!r}")
    width, fps = GIF_SIZES[size]
    src, dst = Path(src), Path(dst)
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled")
    info = probe(src)
    total = max(1, round(info.duration * fps)) if info.duration > 0 else 1
    scale = f"scale={width}:-1:flags=lanczos," if width else ""
    graph = (f"[0:v]fps={fps},{scale}split[a][b];[a]palettegen=stats_mode=diff[p];"
             f"[b][p]paletteuse=dither=bayer:bayer_scale=3")
    partial = dst.with_name(dst.stem + ".partial" + dst.suffix)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
           "-i", str(src), "-an", "-filter_complex", graph, "-loop", "0",
           "-progress", "pipe:1", "-nostats", str(partial)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tail = deque(maxlen=15)

    def drain():
        for line in proc.stderr:
            tail.append(line.decode(errors="replace").rstrip())

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        try:
            for raw in proc.stdout:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                line = raw.decode(errors="replace").strip()
                if line.startswith("frame=") and on_progress is not None:
                    done = int(line.split("=", 1)[1] or 0)
                    on_progress(min(done, total), total)
        except BaseException:
            proc.kill()
            raise
        finally:
            proc.wait()
            reader.join()
        if proc.returncode != 0 or not partial.exists():
            details = "\n".join(tail)
            raise Vid2AsciiError(f"GIF conversion failed for {src.name}:\n{details}")
        os.replace(partial, dst)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    out = probe(dst)
    frames = max(1, round(out.duration * fps)) if out.duration > 0 else total
    if on_progress is not None:
        on_progress(frames, frames)
    return ConvertResult(dst, out.width, out.height, out.duration, frames)
