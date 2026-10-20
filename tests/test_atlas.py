import numpy as np
import pytest

from vid2ascii import FontError
from vid2ascii.atlas import (
    CHARSETS,
    build_atlas,
    find_font,
    font_cell_ratio,
    resolve_charset,
)


def test_resolve_presets():
    assert resolve_charset("standard") == " .:-=+*#%@"


def test_detailed_preset_length():
    assert len(CHARSETS["detailed"]) == 70


def test_resolve_custom():
    assert resolve_charset("ab") == "ab"


def test_resolve_too_short():
    with pytest.raises(ValueError):
        resolve_charset("x")


def test_find_font_default():
    import os

    assert os.path.isfile(find_font())


def test_find_font_missing():
    with pytest.raises(FontError):
        find_font("nope.ttf")


def test_font_cell_ratio():
    w, h = font_cell_ratio(find_font())
    assert 0 < w < h


def test_atlas_shape_and_density():
    a = build_atlas(" .@", find_font(), 16, 30)
    assert a.shape == (3, 30, 16)
    assert a.dtype == np.float32
    assert a[0].sum() == 0
    assert a[2].sum() > a[1].sum() > 0
    assert 0 <= a.min()
    assert a.max() <= 1


def test_blocks_atlas_builds():
    a = build_atlas(CHARSETS["blocks"], find_font(), 16, 30)
    assert a.shape == (5, 30, 16)
