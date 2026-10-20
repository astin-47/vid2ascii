import subprocess

import pytest

_ENC = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
_AENC = ["-c:a", "aac"]


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                   check=True, capture_output=True)


def _testsrc(w, h, d):
    return ["-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=25:duration={d}"]


def _sine(d):
    return ["-f", "lavfi", "-i", f"sine=frequency=440:duration={d}"]


def _with_audio(path, w, h, d):
    _ffmpeg(*_testsrc(w, h, d), *_sine(d), *_ENC, *_AENC, "-shortest", str(path))
    return path


@pytest.fixture(scope="session")
def clip_169(tmp_path_factory):
    return _with_audio(tmp_path_factory.mktemp("clips") / "clip_169.mp4", 640, 360, 3)


@pytest.fixture(scope="session")
def clip_916(tmp_path_factory):
    return _with_audio(tmp_path_factory.mktemp("clips") / "clip_916.mp4", 360, 640, 2)


@pytest.fixture(scope="session")
def clip_noaudio(tmp_path_factory):
    path = tmp_path_factory.mktemp("clips") / "clip_noaudio.mp4"
    _ffmpeg(*_testsrc(640, 360, 2), *_ENC, "-an", str(path))
    return path


@pytest.fixture(scope="session")
def clip_clicks(tmp_path_factory):
    path = tmp_path_factory.mktemp("clips") / "clip_clicks.mp4"
    expr = "if(lt(mod(t,0.5),0.03),sin(2*PI*1000*t),0)"
    _ffmpeg(*_testsrc(640, 360, 4),
            "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=44100:d=4",
            *_ENC, *_AENC, "-shortest", str(path))
    return path
