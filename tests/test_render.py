import time

import numpy as np
import pytest

from vid2ascii.render import (
    AutoContrast,
    Renderer,
    beat_indices,
    compose,
    luminance,
    map_to_indices,
    tint,
)

ATLAS = np.stack([np.full((4, 2), v, np.float32) for v in (0, 0.5, 1)])


def test_luminance_weights():
    rgb = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255]]], np.uint8)
    lum = luminance(rgb)
    assert lum.dtype == np.float32
    np.testing.assert_allclose(lum[0], [0.2126, 0.7152, 0.0722], atol=1e-6)


def test_map_endpoints():
    assert map_to_indices(np.array([0.0]), 10)[0] == 0
    assert map_to_indices(np.array([1.0]), 10)[0] == 9
    assert map_to_indices(np.array([0.5]), 10)[0] == 5


def test_map_monotonic():
    idx = map_to_indices(np.linspace(0, 1, 100), 10)
    assert np.all(np.diff(idx) >= 0)


def test_compose_shape_and_values():
    idx = np.array([[0, 1, 2], [2, 1, 0]])
    out = compose(idx, ATLAS)
    assert out.shape == (8, 6)
    np.testing.assert_array_equal(out[4:8, 4:6], ATLAS[idx[1, 2]])


def test_tint_green():
    mask = np.ones((8, 6), np.float32)
    rgb = np.full((2, 3, 3), 255, np.uint8)
    out = tint(mask, rgb, "green", 2, 4)
    assert out.shape == (8, 6, 3)
    assert (out == np.array([0x33, 0xFF, 0x66], np.uint8)).all()


def test_tint_black_frame_no_nan():
    mask = np.ones((8, 6), np.float32)
    rgb = np.zeros((2, 3, 3), np.uint8)
    out = tint(mask, rgb, "original", 2, 4)
    assert out.dtype == np.uint8
    assert not np.isnan(out.astype(np.float32)).any()


def test_autocontrast_lifts_dark():
    rng = np.random.default_rng(0)
    lum = rng.uniform(0.05, 0.15, (30, 40)).astype(np.float32)
    out = AutoContrast("auto")(lum)
    assert out.mean() > 0.35
    assert out.max() >= 0.9


def test_autocontrast_none_identity():
    lum = np.random.default_rng(1).random((5, 5)).astype(np.float32)
    np.testing.assert_array_equal(AutoContrast("none")(lum), lum)


def test_autocontrast_float_and_invalid():
    lum = np.full((2, 2), 0.6, np.float32)
    np.testing.assert_allclose(AutoContrast("2")(lum), 0.7, atol=1e-6)
    with pytest.raises(ValueError):
        AutoContrast("bogus")


def test_beat_indices_changes_and_identity():
    idx = np.zeros((20, 20), np.int64)
    rng = np.random.default_rng(0)
    same = beat_indices(idx, 0.0, 3, rng)
    np.testing.assert_array_equal(same, idx)
    out = beat_indices(idx, 1.0, 3, rng)
    assert out.mean() > idx.mean()


def test_renderer_output():
    r = Renderer(ATLAS)
    frame = np.random.default_rng(0).integers(0, 256, (3, 4, 3), dtype=np.uint8)
    out = r.render(frame)
    assert out.dtype == np.uint8
    assert out.shape == (12, 8, 3)


def test_render_perf():
    atlas = np.random.default_rng(0).random((10, 28, 16)).astype(np.float32)
    r = Renderer(atlas)
    frame = np.random.default_rng(1).integers(0, 256, (68, 120, 3), dtype=np.uint8)
    r.render(frame)
    t = time.perf_counter()
    for _ in range(10):
        r.render(frame)
    assert (time.perf_counter() - t) / 10 < 0.05
