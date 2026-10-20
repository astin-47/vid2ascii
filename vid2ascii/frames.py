"""Frame geometry (pure math) and, later, ffmpeg probing/decoding."""
from dataclasses import dataclass

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


def _even_up(x: int) -> int:
    return x + (x % 2)


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
    cw = _even_up(max(4, round(w / cols)))
    ch = _even_up(max(4, round(cw * font_h / font_w)))
    rows = max(1, round(h / ch))
    return Layout(cols, rows, cw, ch, cols * cw, rows * ch)


def crop_filter(src_w: int, src_h: int, aspect: float) -> str:
    src_aspect = src_w / src_h
    if abs(src_aspect - aspect) / aspect < 0.01:
        return ""
    if src_aspect > aspect:
        return f"crop={_even_down(round(src_h * aspect))}:{src_h}"
    return f"crop={src_w}:{_even_down(round(src_w / aspect))}"
