"""Command line interface: decode -> render -> encode, preview and folder modes."""

import argparse
import math
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image
from tqdm import tqdm

from . import InputError, Vid2AsciiError
from .atlas import build_atlas, find_font, font_cell_ratio, resolve_charset
from .frames import compute_layout, iter_frames, parse_aspect, probe, require_ffmpeg
from .render import Renderer

MAX_DURATION = 60.0
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _number(cast, minimum, strict):
    def parse(text):
        try:
            value = cast(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid number: {text!r}") from None
        if value < minimum or (strict and value == minimum):
            op = ">" if strict else ">="
            raise argparse.ArgumentTypeError(f"must be {op} {minimum}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vid2ascii", description="Turn a video (or a folder of videos) into ASCII art.")
    p.add_argument("input", help="video file or folder of videos")
    p.add_argument("-o", "--output", help="output file (folder mode: output folder)")
    p.add_argument("--start", type=_number(float, 0, False), default=0.0,
                   help="start time in seconds")
    p.add_argument("--duration", type=_number(float, 0, True), default=None,
                   help="seconds to convert (default: whole clip, max 60)")
    p.add_argument("--fps", type=_number(float, 0, True), default=24.0)
    p.add_argument("--cols", type=_number(int, 1, False), default=120)
    p.add_argument("--color", choices=["original", "green", "amber", "mono"],
                   default="original")
    p.add_argument("--charset", default="standard",
                   help="standard|detailed|blocks|binary or custom characters")
    p.add_argument("--contrast", default="auto", help="auto|none|<factor>")
    p.add_argument("--beat", action="store_true", help="flash on detected beats")
    p.add_argument("--preview", type=_number(int, 0, False), default=0, metavar="N",
                   help="write N preview PNGs instead of a video")
    p.add_argument("--aspect", default=None,
                   help="source|reel|square|wide|cinematic|WxH (center crop)")
    p.add_argument("--font", default=None, help="monospace TTF/TTC font path")
    p.add_argument("--overwrite", action="store_true", help="replace an existing output")
    return p


def _beat_envelope(input_path, info, start, dur, n_frames, fps):
    if not info.has_audio:
        print(f"warning: {input_path.name} has no audio; --beat ignored", file=sys.stderr)
        return None
    from .audio import beat_envelope, detect_beats, load_audio

    sr = 22050
    samples = load_audio(input_path, start, dur, sr=sr)
    return beat_envelope(detect_beats(samples, sr), n_frames, fps)


def _render_frames(frames, renderer, envelope, n_frames):
    """Yield (index, rendered frame) for at most n_frames decoded frames."""
    for i, cells in enumerate(frames):
        if i >= n_frames:
            break
        strength = float(envelope[i]) if envelope is not None else 0.0
        yield i, renderer.render(cells, strength)


def _write_previews(frames, renderer, envelope, n_frames, count, output_path):
    """Render only the N evenly spaced frames and save them as PNGs."""
    indices = np.round(np.linspace(0, n_frames - 1, count)).astype(int)
    written = 0
    for i, cells in enumerate(frames):
        targets = np.flatnonzero(indices == i)
        if targets.size:
            strength = float(envelope[i]) if envelope is not None else 0.0
            frame = renderer.render(cells, strength)
            for j in targets:
                name = f"{output_path.stem}_preview_{j + 1:02d}.png"
                Image.fromarray(frame).save(output_path.parent / name)
                written += 1
        if i >= indices[-1]:
            break
    return written


def _encode(rendered, output_path, input_path, info, layout, fps, start, dur, n_frames):
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{layout.out_w}x{layout.out_h}", "-r", str(fps), "-i", "-"]
    if info.has_audio:
        cmd += ["-ss", str(start), "-t", str(dur), "-i", str(input_path),
                "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-shortest", "-movflags", "+faststart", str(output_path)]
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
        with tqdm(total=n_frames, unit="frame", file=sys.stderr, disable=None) as bar:
            for _, frame in rendered:
                try:
                    proc.stdin.write(frame.data)
                except OSError:  # BrokenPipeError, or EINVAL on Windows
                    broken = True
                    break
                count += 1
                bar.update(1)
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
        raise Vid2AsciiError(f"ffmpeg produced no frames for {input_path}")
    if broken or proc.returncode != 0:
        raise Vid2AsciiError(f"ffmpeg encoding failed for {output_path}:\n{details}")
    return count


def convert(input_path: Path, output_path: Path, args: argparse.Namespace) -> Path:
    input_path, output_path = Path(input_path), Path(output_path)
    if not args.preview and output_path.exists() and not args.overwrite:
        raise InputError(f"output exists: {output_path} (use --overwrite)")
    info = probe(input_path)
    start = args.start
    dur = min(MAX_DURATION, args.duration or math.inf, info.duration - start)
    if dur <= 0:
        raise InputError("--start is past the end of the video")

    chars = resolve_charset(args.charset)
    aspect = parse_aspect(args.aspect, info.width, info.height)
    font = find_font(args.font)
    layout = compute_layout(aspect, args.cols, *font_cell_ratio(font))
    renderer = Renderer(build_atlas(chars, font, layout.cw, layout.ch),
                        color=args.color, contrast=args.contrast)

    fps = args.fps
    n_frames = max(1, round(dur * fps))
    envelope = (_beat_envelope(input_path, info, start, dur, n_frames, fps)
                if args.beat else None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames = iter_frames(input_path, info, start, dur, fps, layout, aspect)
    rendered = _render_frames(frames, renderer, envelope, n_frames)
    try:
        if args.preview:
            written = _write_previews(frames, renderer, envelope, n_frames, args.preview,
                                      output_path)
            if written == 0:
                raise Vid2AsciiError(f"ffmpeg produced no frames for {input_path}")
            print(f"{written} previews in {output_path.parent} "
                  f"({layout.out_w}x{layout.out_h})")
            return output_path.parent
        count = _encode(rendered, output_path, input_path, info, layout, fps, start, dur,
                        n_frames)
    finally:
        rendered.close()
        frames.close()
    print(f"{output_path}  {layout.out_w}x{layout.out_h}  {count / fps:.2f}s  {count} frames")
    return output_path


def _convert_folder(folder: Path, args: argparse.Namespace) -> int:
    out_dir = Path(args.output) if args.output else folder / "ascii_out"
    videos = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    if not videos:
        raise InputError(f"no video files in {folder}")
    failures = []
    for video in videos:
        try:
            convert(video, out_dir / f"{video.stem}_ascii.mp4", args)
        except Vid2AsciiError as exc:
            print(f"error: {video.name}: {exc}", file=sys.stderr)
            failures.append(video.name)
    print(f"converted {len(videos) - len(failures)}/{len(videos)} files into {out_dir}")
    if failures:
        print("failed: " + ", ".join(failures), file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_ffmpeg()
        src = Path(args.input)
        if src.is_dir():
            return _convert_folder(src, args)
        out = Path(args.output) if args.output else src.with_name(f"{src.stem}_ascii.mp4")
        convert(src, out, args)
        return 0
    except (Vid2AsciiError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
