import shutil
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from vid2ascii.web import files as files_mod
from vid2ascii.web.files import FileStore
from vid2ascii.web.server import create_app

ODD = "my clip ü #1%.mp4"


@pytest.fixture
def env(clip_169, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_169, lib / "a.mp4")
    shutil.copy(clip_169, lib / ODD)
    (lib / "notes.txt").write_text("not a video")
    work = tmp_path / "work"
    app = create_app(lib, work)
    with TestClient(app) as client:
        yield client, app, lib, work


def url(file_id, tail):
    return f"/api/files/{quote(file_id, safe='')}/{tail}"


def test_options_shape(env):
    client, *_ = env
    data = client.get("/api/options").json()
    assert data["defaults"]["cols"] == 120
    for key in ("colors", "charsets", "aspects", "limits"):
        assert key in data
    assert data["limits"]["max_duration"] == 60
    assert data["limits"]["max_upload_bytes"] == 2 * 1024 ** 3


def test_library_lists_videos(env):
    client, *_ = env
    items = client.get("/api/library").json()["items"]
    assert sorted(i["name"] for i in items) == sorted(["a.mp4", ODD])
    a = next(i for i in items if i["name"] == "a.mp4")
    assert a["id"] == "lib:a.mp4"
    assert a["duration"] == pytest.approx(3, abs=0.1)
    assert a["width"] == 640 and a["has_audio"] is True


def test_library_missing_folder(tmp_path):
    app = create_app(tmp_path / "nope", tmp_path / "work")
    with TestClient(app) as client:
        data = client.get("/api/library").json()
    assert data["items"] == []
    assert data["warning"]


def test_library_odd_names_roundtrip(env):
    client, *_ = env
    item = next(i for i in client.get("/api/library").json()["items"] if i["name"] == ODD)
    thumb = client.get(url(item["id"], "thumb"))
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert client.get(url(item["id"], "video")).status_code == 200


@pytest.mark.parametrize("bad_id", [
    "lib:../secret.mp4", "lib:C:/Windows/win.ini", "lib:/etc/passwd",
    "up:../../x.mp4", "out:..\\x.mp4", "zzz:a.mp4", "lib:notes.txt",
])
def test_path_escape_404(env, bad_id):
    client, *_ = env
    response = client.get(url(bad_id, "video"))
    assert response.status_code == 404
    assert "error" in response.json()


def test_upload_ok(env, clip_169):
    client, app, lib, work = env
    with open(clip_169, "rb") as fh:
        response = client.post("/api/upload", files={"file": ("mine.mp4", fh, "video/mp4")})
    assert response.status_code == 200
    data = response.json()
    assert data["id"].startswith("up:")
    assert data["name"] == "mine.mp4"
    assert app.state.files.resolve(data["id"]).is_file()


def test_upload_rejects_non_video(env):
    client, app, *_ = env
    response = client.post("/api/upload",
                           files={"file": ("fake.mp4", b"hello" * 100, "video/mp4")})
    assert response.status_code == 400
    assert "cannot read video" in response.json()["error"]
    assert list(app.state.files.uploads.iterdir()) == []


def test_upload_too_large(env, monkeypatch):
    client, app, *_ = env
    monkeypatch.setattr(files_mod, "MAX_UPLOAD_BYTES", 1000)
    response = client.post("/api/upload",
                           files={"file": ("big.mp4", b"x" * 5000, "video/mp4")})
    assert response.status_code == 413
    assert list(app.state.files.uploads.iterdir()) == []


def test_video_range(env):
    client, *_ = env
    response = client.get(url("lib:a.mp4", "video"), headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100
    assert "content-range" in response.headers


def test_download_headers(env):
    client, *_ = env
    response = client.get(url("lib:a.mp4", "download"))
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]


def test_next_output_path_never_overwrites(tmp_path):
    store = FileStore(tmp_path / "lib", tmp_path / "work")
    assert store.next_output_path("x").name == "x_ascii.mp4"
    (store.outputs / "x_ascii.mp4").write_bytes(b"")
    assert store.next_output_path("x").name == "x_ascii_2.mp4"


def test_outputs_listed(env, clip_169):
    client, app, *_ = env
    shutil.copy(clip_169, app.state.files.outputs / "a_ascii.mp4")
    items = client.get("/api/outputs").json()["items"]
    assert [i["id"] for i in items] == ["out:a_ascii.mp4"]
    assert "created" in items[0]
