import json
import shutil
import subprocess

import numpy as np
import pytest

from vid2ascii.atlas import find_font, font_cell_ratio
from vid2ascii.cli import main
from vid2ascii.frames import compute_layout


def ffprobe_json(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, check=True)
    return json.loads(result.stdout)


def _video(info):
    return next(s for s in info["streams"] if s["codec_type"] == "video")


def _has_audio(info):
    return any(s["codec_type"] == "audio" for s in info["streams"])


def test_convert_basic(clip_169, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--duration", "2", "--cols", "80"]) == 0
    info = ffprobe_json(out)
    assert abs(float(info["format"]["duration"]) - 2) <= 0.15
    video = _video(info)
    assert video["codec_name"] == "h264"
    assert _has_audio(info)
    layout = compute_layout(16 / 9, 80, *font_cell_ratio(find_font()))
    assert (video["width"], video["height"]) == (layout.out_w, layout.out_h)


def test_convert_noaudio(clip_noaudio, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_noaudio), "-o", str(out), "--cols", "40"]) == 0
    assert not _has_audio(ffprobe_json(out))


def test_aspect_reel(clip_169, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--aspect", "reel", "--cols", "60",
                 "--duration", "1"]) == 0
    video = _video(ffprobe_json(out))
    assert video["height"] > video["width"]
    assert abs(video["width"] / video["height"] - 0.5625) < 0.05


def test_source_aspect_portrait(clip_916, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_916), "-o", str(out), "--cols", "40", "--duration", "1"]) == 0
    video = _video(ffprobe_json(out))
    assert video["height"] > video["width"]


def test_preview(clip_169, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--preview", "3", "--cols", "40"]) == 0
    pngs = sorted(p.name for p in tmp_path.glob("*.png"))
    assert pngs == ["out_preview_01.png", "out_preview_02.png", "out_preview_03.png"]
    assert not list(tmp_path.glob("*.mp4"))


def _frame_brightness(path):
    """Decode the video at a small size and return mean brightness per frame."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=160:-2",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True)
    w, h = 160, None
    video = _video(ffprobe_json(path))
    h = round(video["height"] * 160 / video["width"] / 2) * 2
    frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, h, w, 3)
    return frames.reshape(len(frames), -1).mean(axis=1)


def test_beat_mode(clip_clicks, tmp_path):
    out = tmp_path / "out.mp4"
    # The beat boost is applied after contrast; disabling contrast here isolates
    # the boost (test_beat_mode_default_contrast covers the default "auto").
    assert main([str(clip_clicks), "-o", str(out), "--beat", "--preview", "0",
                 "--color", "mono", "--contrast", "none", "--cols", "60"]) == 0
    brightness = _frame_brightness(out)
    fps = 24
    # Detected beats lag the clicks slightly (~30 ms, about one frame), so take the
    # brightest frame within 0.1 s after each beat.
    beats = [round(k * 0.5 * fps) for k in range(1, 7)]
    on_beat = [brightness[i:i + 3].max() for i in beats]
    off_beat = [brightness[i + round(0.25 * fps)] for i in beats]
    assert np.mean(on_beat) > np.mean(off_beat)


def test_folder_mode(clip_169, clip_noaudio, tmp_path):
    folder = tmp_path / "videos"
    folder.mkdir()
    shutil.copy(clip_169, folder / "a.mp4")
    shutil.copy(clip_noaudio, folder / "b.mp4")
    assert main([str(folder), "--cols", "30", "--duration", "1"]) == 0
    outs = sorted(p.name for p in (folder / "ascii_out").glob("*_ascii.mp4"))
    assert outs == ["a_ascii.mp4", "b_ascii.mp4"]


def test_bad_file_exit2(tmp_path, capsys):
    bad = tmp_path / "bad.mp4"
    bad.write_text("not a video")
    assert main([str(bad)]) == 2
    assert "cannot read video" in capsys.readouterr().err


def test_bad_font_exit2(clip_169, tmp_path):
    assert main([str(clip_169), "-o", str(tmp_path / "o.mp4"), "--font", "nope.ttf"]) == 2


def test_start_past_end(clip_169, tmp_path):
    assert main([str(clip_169), "-o", str(tmp_path / "o.mp4"), "--start", "99"]) == 2


def test_path_with_spaces(clip_169, tmp_path):
    src = tmp_path / "my clip ü.mp4"
    shutil.copy(clip_169, src)
    assert main([str(src), "--cols", "30", "--duration", "1"]) == 0
    assert (tmp_path / "my clip ü_ascii.mp4").is_file()


def test_existing_output_needs_overwrite(clip_169, tmp_path, capsys):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"existing")
    args = [str(clip_169), "-o", str(out), "--cols", "30", "--duration", "1"]
    assert main(args) == 2
    assert "error:" in capsys.readouterr().err
    assert out.read_bytes() == b"existing"
    assert main(args + ["--overwrite"]) == 0
    assert _video(ffprobe_json(out))["codec_name"] == "h264"


def test_duration_capped_60(clip_169, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--duration", "999", "--cols", "30"]) == 0
    assert abs(float(ffprobe_json(out)["format"]["duration"]) - 3) <= 0.15


@pytest.mark.parametrize("opt", [["--fps", "0"], ["--cols", "0"], ["--preview", "-1"]])
def test_invalid_numbers_exit2(clip_169, opt):
    with pytest.raises(SystemExit) as exc:
        main([str(clip_169), *opt])
    assert exc.value.code == 2


def test_preview_renders_only_selected_frames(clip_169, tmp_path, monkeypatch):
    from vid2ascii.render import Renderer

    calls = []
    original = Renderer.render

    def counting_render(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Renderer, "render", counting_render)
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--preview", "3", "--cols", "40"]) == 0
    assert len(calls) == 3


def test_aspect_reel_default_cols(clip_169, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--aspect", "reel", "--duration", "1"]) == 0
    video = _video(ffprobe_json(out))
    assert abs(video["width"] / video["height"] - 0.5625) <= 0.02


def test_crop_uses_realized_aspect(clip_169, tmp_path, monkeypatch):
    import vid2ascii.engine as engine_mod

    seen = []
    original = engine_mod.iter_frames

    def recording(path, info, start, duration, fps, layout, aspect):
        seen.append((layout, aspect))
        return original(path, info, start, duration, fps, layout, aspect)

    monkeypatch.setattr(engine_mod, "iter_frames", recording)
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--aspect", "reel", "--preview", "1"]) == 0
    layout, aspect = seen[0]
    assert aspect == pytest.approx(layout.out_w / layout.out_h)


def _failing_render(monkeypatch, exc, after):
    from vid2ascii.render import Renderer

    calls = []
    original = Renderer.render

    def render(self, *args, **kwargs):
        calls.append(1)
        if len(calls) > after:
            raise exc
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Renderer, "render", render)


def test_failure_mid_encode_leaves_no_output(clip_169, tmp_path, monkeypatch):
    from vid2ascii import Vid2AsciiError

    _failing_render(monkeypatch, Vid2AsciiError("boom"), after=10)
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--cols", "30", "--duration", "2"]) == 2
    assert not out.exists()
    assert not (tmp_path / "out.partial.mp4").exists()
    assert list(tmp_path.iterdir()) == []


def test_interrupt_mid_encode_leaves_no_output(clip_169, tmp_path, monkeypatch, capsys):
    _failing_render(monkeypatch, KeyboardInterrupt(), after=10)
    out = tmp_path / "out.mp4"
    assert main([str(clip_169), "-o", str(out), "--cols", "30", "--duration", "2"]) == 130
    assert "interrupted" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_video_without_stored_duration(tmp_path):
    src = tmp_path / "nodur.webm"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=size=320x180:rate=25:duration=2", "-c:v", "libvpx", "-f", "webm",
         "pipe:1"], capture_output=True, check=True)
    src.write_bytes(result.stdout)
    from vid2ascii.frames import probe

    print("probed duration:", probe(src).duration)  # 0.0 when ffmpeg stores none
    out = tmp_path / "out.mp4"
    assert main([str(src), "-o", str(out), "--cols", "30"]) == 0
    assert abs(float(ffprobe_json(out)["format"]["duration"]) - 2) <= 0.15


def test_beat_mode_default_contrast(clip_clicks, tmp_path):
    out = tmp_path / "out.mp4"
    assert main([str(clip_clicks), "-o", str(out), "--beat", "--color", "mono",
                 "--cols", "60"]) == 0
    brightness = _frame_brightness(out)
    fps = 24
    beats = [round(k * 0.5 * fps) for k in range(1, 7)]
    on_beat = [brightness[i:i + 3].max() for i in beats]
    off_beat = [brightness[i + round(0.25 * fps)] for i in beats]
    assert np.mean(on_beat) / np.mean(off_beat) > 1.1


def test_folder_mode_continues_after_unexpected_error(clip_169, tmp_path, monkeypatch,
                                                     capsys):
    import vid2ascii.cli as cli

    folder = tmp_path / "videos"
    folder.mkdir()
    shutil.copy(clip_169, folder / "a.mp4")
    shutil.copy(clip_169, folder / "b.mp4")
    original = cli.convert

    def convert(src, out, args):
        if src.name == "a.mp4":
            raise RuntimeError("kaboom")
        return original(src, out, args)

    monkeypatch.setattr(cli, "convert", convert)
    assert main([str(folder), "--cols", "30", "--duration", "1"]) == 1
    assert "a.mp4: error: kaboom" in capsys.readouterr().err
    assert (folder / "ascii_out" / "b_ascii.mp4").is_file()


def test_url_input_rejected(tmp_path, capsys):
    assert main(["http://127.0.0.1:9/clip.mp4", "-o", str(tmp_path / "o.mp4")]) == 2
    assert "input not found" in capsys.readouterr().err
