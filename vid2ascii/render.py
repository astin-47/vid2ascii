"""Pure-numpy renderer: cell colors -> full-size ASCII frame."""

import math

import numpy as np

COLORS = {"green": (0x33, 0xFF, 0x66), "amber": (0xFF, 0xB0, 0x00), "mono": (255, 255, 255)}

_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], np.float32)


def luminance(rgb: np.ndarray) -> np.ndarray:
    return ((rgb.astype(np.float32) / 255.0) @ _WEIGHTS).astype(np.float32)


class AutoContrast:
    def __init__(self, mode="auto"):
        self._lo = self._hi = None
        if isinstance(mode, str) and mode in ("auto", "none"):
            self.mode = mode
            return
        try:
            self.mode = float(mode)
        except (TypeError, ValueError):
            raise ValueError(f"invalid contrast: {mode!r}") from None
        if not math.isfinite(self.mode):
            raise ValueError(f"invalid contrast: {mode!r}")

    def __call__(self, lum: np.ndarray) -> np.ndarray:
        if self.mode == "none":
            return lum
        if self.mode != "auto":
            return np.clip((lum - 0.5) * self.mode + 0.5, 0.0, 1.0).astype(np.float32)
        p2, p98 = np.percentile(lum, (2, 98))
        if self._lo is None:
            self._lo, self._hi = float(p2), float(p98)
        else:
            self._lo += 0.2 * (p2 - self._lo)
            self._hi += 0.2 * (p98 - self._hi)
        span = max(self._hi - self._lo, 1e-3)
        out = np.clip((lum - self._lo) / span, 0.0, 1.0).astype(np.float32)
        if out.mean() < 0.35:
            out = out ** 0.7
        return out


def map_to_indices(lum: np.ndarray, n_chars: int) -> np.ndarray:
    return np.clip(np.floor(lum * n_chars), 0, n_chars - 1).astype(np.int64)


def compose(indices: np.ndarray, atlas: np.ndarray) -> np.ndarray:
    rows, cols = indices.shape
    _, ch, cw = atlas.shape
    return atlas[indices].transpose(0, 2, 1, 3).reshape(rows * ch, cols * cw)


def tint(mask: np.ndarray, rgb_cells: np.ndarray, mode: str, cw: int, ch: int) -> np.ndarray:
    rows, cols = rgb_cells.shape[:2]
    if mode == "original":
        c = rgb_cells.astype(np.float32) / 255.0
        m = c.max(axis=2, keepdims=True)
        bright = c * (np.maximum(m, 0.35) / np.maximum(m, 1e-6))
        color = np.where(m < 1e-6, np.float32(0.35), bright) * np.float32(255.0)
    else:
        color = np.broadcast_to(np.array(COLORS[mode], np.float32), (rows, cols, 3))
    # Broadcast each cell color over its ch x cw block (same as np.repeat on both axes),
    # writing each channel straight into the uint8 output to avoid big float temporaries.
    out = np.empty((rows * ch, cols * cw, 3), np.uint8)
    o = out.reshape(rows, ch, cols, cw, 3)
    m = mask.reshape(rows, ch, cols, cw)
    for k in range(3):
        np.multiply(m, color[:, None, :, k, None], out=o[..., k], casting="unsafe")
    return out


def beat_indices(indices, strength, n_chars, rng):
    if strength <= 0:
        return indices
    shift = round(strength * n_chars * 0.3)
    out = np.clip(indices + shift, 0, n_chars - 1)
    sparkle = rng.random(out.shape) < 0.15 * strength
    return np.where(sparkle, rng.integers(1, n_chars, size=out.shape), out)


class Renderer:
    def __init__(self, atlas, color="original", contrast="auto", seed=0):
        self.atlas = atlas
        self.n, self.ch, self.cw = atlas.shape
        self.color = color
        self.contrast = AutoContrast(contrast)
        self.rng = np.random.default_rng(seed)

    def render(self, rgb_cells, beat_strength=0.0):
        lum = self.contrast(luminance(rgb_cells))
        lum = np.clip(lum + 0.35 * beat_strength, 0.0, 1.0)
        idx = beat_indices(map_to_indices(lum, self.n), beat_strength, self.n, self.rng)
        mask = compose(idx, self.atlas)
        return tint(mask, rgb_cells, self.color, self.cw, self.ch)
