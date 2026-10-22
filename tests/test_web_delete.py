import shutil
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from vid2ascii.engine import Options
from vid2ascii.web.server import create_app


@pytest.fixture
def env(clip_169, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_169, lib / "a.mp4")
    shutil.copy(clip_169, lib / "b.mp4")
    trashed = []
    app = create_app(lib, tmp_path / "work", trash=lambda path: trashed.append(path) or path.unlink())
    with TestClient(app) as client:
        yield client, app, lib, trashed


def delete(client, file_id):
    return client.delete(f"/api/files/{quote(file_id, safe='')}")


def test_delete_library_file_goes_to_trash(env):
    client, app, lib, trashed = env
    response = delete(client, "lib:a.mp4")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert trashed == [(lib / "a.mp4").resolve()]
    assert not (lib / "a.mp4").exists()
    names = [i["name"] for i in client.get("/api/library").json()["items"]]
    assert names == ["b.mp4"]


def test_delete_upload_and_render(env, clip_169):
    client, app, lib, trashed = env
    with open(clip_169, "rb") as fh:
        up = client.post("/api/upload", files={"file": ("mine.mp4", fh, "video/mp4")}).json()
    out = app.state.files.outputs / "a_ascii.mp4"
    shutil.copy(clip_169, out)
    assert delete(client, up["id"]).status_code == 200
    assert delete(client, "out:a_ascii.mp4").status_code == 200
    assert list(app.state.files.uploads.iterdir()) == []
    assert not out.exists()
    assert len(trashed) == 2


@pytest.mark.parametrize("bad_id", ["lib:../a.mp4", "lib:nope.mp4", "zzz:a.mp4", r"out:..\x.mp4"])
def test_delete_unknown_or_escaping_404(env, bad_id):
    client, app, lib, trashed = env
    assert delete(client, bad_id).status_code == 404
    assert trashed == []
    assert (lib / "a.mp4").exists()


def test_delete_refused_while_rendering(env):
    client, app, lib, trashed = env
    jobs = app.state.jobs
    job_id = jobs.submit("lib:a.mp4", Options(duration=3))
    while jobs.get(job_id).state == "queued":
        time.sleep(0.01)
    response = delete(client, "lib:a.mp4")
    assert response.status_code == 409
    assert "render" in response.json()["error"]
    assert trashed == []
    jobs.cancel(job_id)


def test_delete_cross_site_refused(env):
    client, app, lib, trashed = env
    response = client.delete("/api/files/lib%3Aa.mp4", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert trashed == []


def test_default_trash_is_send2trash(tmp_path):
    import send2trash

    from vid2ascii.web.files import FileStore

    store = FileStore(tmp_path / "lib", tmp_path / "work")
    assert store.trash_fn is send2trash.send2trash
