import os
import shutil
import subprocess
import time

import numpy as np
import pytest

import vid2ascii.engine as engine
from vid2ascii.engine import Options, PreviewCache, plan, render_still
from vid2ascii.frames import Layout, decode_frame, probe


@pytest.fixture(scope="session")
def clip_720p(tmp_path_factory):
    path = tmp_path_factory.mktemp("hd") / "hd.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)
    return path


def counting_decoder(monkeypatch):
    calls = []
    original = engine.decode_frame

    def wrapper(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "decode_frame", wrapper)
    return calls


def test_decode_frame_shape(clip_169):
    info = probe(clip_169)
    layout = Layout(cols=40, rows=12, cw=8, ch=16, out_w=320, out_h=192)
    cells = decode_frame(clip_169, info, 1.0, layout, 640 / 360)
    assert cells.shape == (12, 40, 3)
    assert cells.dtype == np.uint8


def test_decode_frame_past_end_falls_back(clip_169):
    info = probe(clip_169)
    layout = Layout(cols=40, rows=12, cw=8, ch=16, out_w=320, out_h=192)
    assert decode_frame(clip_169, info, 99.0, layout, 640 / 360).shape == (12, 40, 3)


def test_render_still_shape(clip_169):
    opts = Options()
    p = plan(clip_169, opts)
    img = render_still(clip_169, 1.0, opts, cache=PreviewCache())
    assert img.shape == (p.layout.out_h, p.layout.out_w, 3)
    assert img.dtype == np.uint8
    draft = render_still(clip_169, 1.0, opts, long_side=960, cache=PreviewCache())
    assert max(draft.shape[:2]) <= 1100


def test_cache_reuses_frame_on_color_change(clip_169, monkeypatch):
    calls = counting_decoder(monkeypatch)
    cache = PreviewCache()
    render_still(clip_169, 1.0, Options(color="original"), cache=cache)
    render_still(clip_169, 1.0, Options(color="green"), cache=cache)
    assert len(calls) == 1


def test_cache_invalidates_on_mtime(clip_169, clip_916, tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    shutil.copy(clip_169, src)
    calls = counting_decoder(monkeypatch)
    cache = PreviewCache()
    first = render_still(src, 1.0, Options(), cache=cache)
    shutil.copy(clip_916, src)
    later = time.time() + 5
    os.utime(src, (later, later))
    second = render_still(src, 1.0, Options(), cache=cache)
    assert len(calls) == 2
    assert first.shape != second.shape


def test_still_latency_budget(clip_720p):
    cache = PreviewCache()
    render_still(clip_720p, 2.0, Options(), cache=cache)  # warm up: decode + atlas

    t0 = time.perf_counter()
    render_still(clip_720p, 2.0, Options(color="green"), cache=cache)
    setting_change_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    render_still(clip_720p, 6.5, Options(), long_side=960, cache=cache)
    new_time_draft_ms = (time.perf_counter() - t0) * 1000

    assert setting_change_ms <= 100, f"setting change took {setting_change_ms:.0f} ms"
    assert new_time_draft_ms <= 300, f"new-time draft took {new_time_draft_ms:.0f} ms"
