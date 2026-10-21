import threading

import pytest

from vid2ascii import Cancelled
from vid2ascii.engine import Options, convert, write_previews


def test_options_validate_ok():
    Options().validate()


@pytest.mark.parametrize("bad", [
    {"fps": 0}, {"cols": 0}, {"start": -1}, {"duration": 0},
    {"color": "pink"}, {"charset": "x"}, {"contrast": "nan"},
])
def test_options_validate_errors(bad):
    with pytest.raises(ValueError):
        Options(**bad).validate()


def test_options_from_dict():
    opts = Options.from_dict({"cols": "80", "fps": "30", "beat": True, "zzz": 1})
    assert opts.cols == 80
    assert opts.fps == 30.0
    assert opts.beat is True


def test_convert_progress(clip_169, tmp_path):
    calls = []
    out = tmp_path / "out.mp4"
    result = convert(clip_169, out, Options(duration=1, cols=40),
                     on_progress=lambda done, total: calls.append((done, total)))
    assert out.exists()
    assert result.path == out
    assert calls, "on_progress was never called"
    assert calls[-1] == (result.frames, result.frames)
    assert result.width > 0 and result.height > 0


def test_convert_cancel(clip_169, tmp_path):
    cancel = threading.Event()
    calls = []

    def progress(done, total):
        calls.append(done)
        if len(calls) == 3:
            cancel.set()

    with pytest.raises(Cancelled):
        convert(clip_169, tmp_path / "out.mp4", Options(duration=2, cols=40),
                on_progress=progress, cancel=cancel)
    assert list(tmp_path.iterdir()) == []


def test_write_previews_returns_paths(clip_169, tmp_path):
    paths = write_previews(clip_169, tmp_path / "pv.mp4", Options(cols=40), 2)
    assert len(paths) == 2
    assert all(p.exists() and p.suffix == ".png" for p in paths)
