import pytest


@pytest.mark.parametrize("name", ["clip_169", "clip_916", "clip_noaudio", "clip_clicks"])
def test_fixture_exists(name, request):
    path = request.getfixturevalue(name)
    assert path.exists()
    assert path.stat().st_size > 1000
