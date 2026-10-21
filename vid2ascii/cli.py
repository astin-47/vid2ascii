"""Command line interface: argument parsing, preview and folder modes over the engine."""

import argparse
import sys
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from . import InputError, Vid2AsciiError, engine
from .engine import Options
from .frames import require_ffmpeg

MAX_DURATION = engine.MAX_DURATION
VIDEO_EXTS = engine.VIDEO_EXTS


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


def _options(args: argparse.Namespace) -> Options:
    return Options(start=args.start, duration=args.duration, fps=args.fps, cols=args.cols,
                   color=args.color, charset=args.charset, contrast=args.contrast,
                   beat=args.beat, aspect=args.aspect, font=args.font)


def convert(input_path: Path, output_path: Path, args: argparse.Namespace) -> Path:
    input_path, output_path = Path(input_path), Path(output_path)
    opts = _options(args)
    if args.preview:
        paths = engine.write_previews(input_path, output_path, opts, args.preview)
        width, height = Image.open(paths[0]).size
        print(f"{len(paths)} previews in {output_path.parent} ({width}x{height})")
        return output_path.parent
    bar = None

    def progress(done, total):
        nonlocal bar
        if bar is None:  # opened lazily, so errors and warnings print before any bar
            bar = tqdm(total=total, unit="frame", file=sys.stderr, disable=None)
        bar.update(done - bar.n)

    try:
        result = engine.convert(input_path, output_path, opts, overwrite=args.overwrite,
                                on_progress=progress)
    finally:
        if bar is not None:
            bar.close()
    print(f"{result.path}  {result.width}x{result.height}  {result.duration:.2f}s  "
          f"{result.frames} frames")
    return result.path


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
        except Exception as exc:
            print(f"{video.name}: error: {exc}", file=sys.stderr)
            failures.append(video.name)
    print(f"converted {len(videos) - len(failures)}/{len(videos)} files into {out_dir}")
    if failures:
        print("failed: " + ", ".join(failures), file=sys.stderr)
    return 1 if failures else 0


def build_serve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vid2ascii serve", description="Open the local web interface.",
        epilog="To convert a file literally named 'serve', pass it as ./serve.")
    p.add_argument("--library", type=Path, default=Path.home() / "Videos",
                   help="folder of videos to list in the page (default: ~/Videos)")
    p.add_argument("--workdir", type=Path, default=Path.cwd(),
                   help="where work/ and outputs/ are kept (default: current folder)")
    p.add_argument("--port", type=_number(int, 1, False), default=8765)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    return p


def _serve(argv: list[str]) -> int:
    args = build_serve_parser().parse_args(argv)
    try:
        require_ffmpeg()
    except Vid2AsciiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    from .web import server

    return server.serve(args.library, args.workdir, args.port, not args.no_browser)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["serve"]:
        return _serve(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        require_ffmpeg()
        src = Path(args.input)
        if not (src.is_file() or src.is_dir()):
            raise InputError(f"input not found: {args.input}")
        if src.is_dir():
            return _convert_folder(src, args)
        out = Path(args.output) if args.output else src.with_name(f"{src.stem}_ascii.mp4")
        convert(src, out, args)
        return 0
    except (Vid2AsciiError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
