import subprocess

import pytest

from vid2ascii import InputError
from vid2ascii.frames import Layout, iter_frames, probe


def test_probe_169(clip_169):
    info = probe(clip_169)
    assert (info.width, info.height) == (640, 360)
    assert info.fps == pytest.approx(25, abs=0.1)
    assert info.duration == pytest.approx(3, abs=0.1)
    assert info.has_audio is True


def test_probe_noaudio(clip_noaudio):
    assert probe(clip_noaudio).has_audio is False


def test_probe_bad_file(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_text("not a video")
    with pytest.raises(InputError, match="cannot read video"):
        probe(bad)


def test_probe_missing_file(tmp_path):
    with pytest.raises(InputError, match="cannot read video"):
        probe(tmp_path / "nope.mp4")


def test_iter_frames_count_and_shape(clip_169):
    info = probe(clip_169)
    layout = Layout(40, 12, 16, 32, 640, 384)
    frames = list(iter_frames(clip_169, info, 1, 1, 12, layout,
                              info.width / info.height))
    assert abs(len(frames) - 12) <= 1
    assert all(f.shape == (12, 40, 3) and f.dtype.name == "uint8" for f in frames)


def _make_clip(path, size, rate):
    # yuv444p so odd dimensions (641x359) are accepted by libx264.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"testsrc2=size={size}:rate={rate}:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv444p", str(path)],
        check=True, capture_output=True)
    return path


@pytest.mark.parametrize("size,rate", [("641x359", "30000/1001"), ("64x36", "25")])
def test_iter_frames_odd_source(tmp_path, size, rate):
    clip = _make_clip(tmp_path / "odd.mkv", size, rate)
    info = probe(clip)
    aspect = info.width / info.height
    layout = Layout(40, 12, 16, 32, 640, 384)
    frames = list(iter_frames(clip, info, 0, 1, 24, layout, aspect))
    assert abs(len(frames) - 24) <= 1
    assert all(f.shape == (12, 40, 3) for f in frames)
    # a different target aspect forces the crop path
    frames = list(iter_frames(clip, info, 0, 1, 24, layout, 1.0))
    assert abs(len(frames) - 24) <= 1
    assert all(f.shape == (12, 40, 3) for f in frames)
