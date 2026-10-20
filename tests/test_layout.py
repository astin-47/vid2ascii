import pytest

from vid2ascii.frames import compute_layout, crop_filter, parse_aspect


def test_parse_aspect_source():
    assert parse_aspect(None, 1280, 720) == pytest.approx(16 / 9)
    assert parse_aspect("source", 1280, 720) == pytest.approx(16 / 9)


def test_parse_aspect_presets():
    assert parse_aspect("reel", 1280, 720) == pytest.approx(0.5625)
    assert parse_aspect("cinematic", 1280, 720) == pytest.approx(2.39)


def test_parse_aspect_wxh():
    assert parse_aspect("1080x1350", 1280, 720) == pytest.approx(0.8)
    assert parse_aspect("1080X1350", 1280, 720) == pytest.approx(0.8)


@pytest.mark.parametrize("spec", ["banana", "0x100", "100x0", "-5x10", "axb", "1x2x3", ""])
def test_parse_aspect_bad(spec):
    with pytest.raises(ValueError):
        parse_aspect(spec, 1280, 720)


def test_layout_169():
    lay = compute_layout(16 / 9, 120, 55, 100)
    assert lay.cols == 120
    assert lay.cw % 2 == 0 and lay.ch % 2 == 0
    assert lay.out_w % 2 == 0 and lay.out_h % 2 == 0
    assert abs(lay.out_w / lay.out_h - 16 / 9) < 0.05
    assert lay.ch > lay.cw


def test_layout_reel():
    lay = compute_layout(9 / 16, 60, 55, 100)
    assert lay.out_h > lay.out_w
    assert abs(lay.out_w / lay.out_h - 0.5625) < 0.05
    assert abs(max(lay.out_w, lay.out_h) - 1920) <= lay.ch


@pytest.mark.parametrize("fw", [55, 60])
def test_char_aspect_correction(fw):
    aspect, cols, fh = 16 / 9, 120, 100
    lay = compute_layout(aspect, cols, fw, fh)
    assert abs(lay.rows - lay.cols * lay.cw / (lay.ch * aspect)) <= 1
    assert lay.ch > lay.cw
    assert abs(lay.ch / lay.cw - fh / fw) < 0.15


def test_layout_bad_args():
    with pytest.raises(ValueError):
        compute_layout(0, 120, 55, 100)
    with pytest.raises(ValueError):
        compute_layout(1.0, 0, 55, 100)


def test_crop_none():
    assert crop_filter(1280, 720, 16 / 9) == ""


def test_crop_wide_to_reel():
    assert crop_filter(1280, 720, 9 / 16) == "crop=404:720"


def test_crop_tall_to_wide():
    assert crop_filter(720, 1280, 16 / 9) == "crop=720:404"
