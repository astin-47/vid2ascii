"""Glyph atlas: each character is rendered once; frames are built by indexing."""

import glob
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import FontError

CHARSETS = {
    "standard": " .:-=+*#%@",
    "detailed": " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "blocks": " ░▒▓█",
    "binary": " 01",
}


def resolve_charset(name_or_chars: str) -> str:
    chars = CHARSETS.get(name_or_chars, name_or_chars)
    if len(chars) < 2:
        raise ValueError("charset needs at least 2 characters")
    return chars


def _font_candidates() -> list[str]:
    fonts = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    paths = [os.path.join(fonts, n) for n in ("consola.ttf", "CascadiaMono.ttf", "cour.ttf")]
    paths += [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/Library/Fonts/Courier New.ttf",
    ]
    paths += sorted(glob.glob("/usr/share/fonts/**/DejaVuSansMono.ttf", recursive=True))
    return paths


def find_font(path: str | None = None) -> str:
    if path is not None:
        if not os.path.isfile(path):
            raise FontError(f"font not found: {path}")
        return path
    for candidate in _font_candidates():
        if os.path.isfile(candidate):
            return candidate
    raise FontError("no monospace font found; use --font PATH")


def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(font_path, size)
    except OSError as exc:
        raise FontError(f"cannot load font: {font_path}") from exc


def font_cell_ratio(font_path: str) -> tuple[float, float]:
    """(advance_width, line_height) of the font at size 100."""
    font = _load_font(font_path, 100)
    ascent, descent = font.getmetrics()
    return font.getlength("M"), float(ascent + descent)


def _fits(font: ImageFont.FreeTypeFont, cw: int, ch: int) -> bool:
    ascent, descent = font.getmetrics()
    return ascent + descent <= ch and font.getlength("M") <= cw


def build_atlas(chars: str, font_path: str, cw: int, ch: int) -> np.ndarray:
    """Return float32 (len(chars), ch, cw) coverage masks in [0, 1]."""
    size = 1
    while _fits(_load_font(font_path, size + 1), cw, ch):
        size += 1
    font = _load_font(font_path, size)
    ascent, descent = font.getmetrics()
    x = (cw - font.getlength("M")) / 2
    y = (ch - (ascent + descent)) / 2
    atlas = np.zeros((len(chars), ch, cw), dtype=np.float32)
    for i, c in enumerate(chars):
        img = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(img).text((x, y), c, fill=255, font=font)
        atlas[i] = np.asarray(img, dtype=np.float32) / 255.0
    return atlas
