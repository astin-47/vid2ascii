import json
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from vid2ascii import Cancelled
from vid2ascii.engine import GIF_SIZES, to_gif
from vid2ascii.web.server import create_app


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                          "-show_format", "-of", "json", str(path)],
                         capture_output=True, check=True)
    return json.loads(out.stdout)


def test_gif_sizes():
    assert GIF_SIZES == {"small": (480, 12), "medium": (720, 15), "full": (None, 15)}


def test_to_gif_small(clip_169, tmp_path):
    dst = tmp_path / "a.gif"
    calls = []
    result = to_gif(clip_169, dst, "small", on_progress=lambda d, t: calls.append((d, t)))
    stream = probe(dst)["streams"][0]
    assert stream["codec_name"] == "gif"
    assert stream["width"] == 480
    assert int(stream["nb_read_frames"]) == pytest.approx(3 * 12, abs=2)
    assert result.path == dst and result.width == 480
    assert calls and calls[-1][0] == calls[-1][1]


def test_to_gif_full_keeps_size(clip_169, tmp_path):
    dst = tmp_path / "a.gif"
    to_gif(clip_169, dst, "full")
    assert probe(dst)["streams"][0]["width"] == 640


def test_to_gif_bad_size(clip_169, tmp_path):
    with pytest.raises(ValueError):
        to_gif(clip_169, tmp_path / "a.gif", "huge")


def test_to_gif_cancel_leaves_nothing(clip_169, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        to_gif(clip_169, tmp_path / "a.gif", "small", cancel=cancel)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def env(clip_169, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_169, lib / "a.mp4")
    app = create_app(lib, tmp_path / "work", trash=lambda p: p.unlink())
    shutil.copy(clip_169, app.state.files.outputs / "a_ascii.mp4")
    with TestClient(app) as client:
        yield client, app


def wait_done(app, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = app.state.jobs.get(job_id)
        if job.state in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_export_gif_from_render(env):
    client, app = env
    response = client.post("/api/export/gif", json={"id": "out:a_ascii.mp4", "size": "small"})
    assert response.status_code == 200
    job = wait_done(app, response.json()["job_id"])
    assert job.state == "done", job.message
    assert job.output_id == "out:a_ascii_small.gif"
    items = client.get("/api/outputs").json()["items"]
    gif = next(i for i in items if i["id"] == "out:a_ascii_small.gif")
    assert gif["width"] == 480
    video = client.get(f"/api/files/{quote(gif['id'], safe='')}/video")
    assert video.status_code == 200
    assert video.headers["content-type"] == "image/gif"


def test_export_gif_twice_never_overwrites(env):
    client, app = env
    ids = []
    for _ in range(2):
        job_id = client.post("/api/export/gif", json={"id": "out:a_ascii.mp4", "size": "small"}
                             ).json()["job_id"]
        ids.append(wait_done(app, job_id).output_id)
    assert ids == ["out:a_ascii_small.gif", "out:a_ascii_small_2.gif"]


@pytest.mark.parametrize("body, status", [
    ({"id": "lib:a.mp4", "size": "small"}, 400),        # only finished renders
    ({"id": "out:a_ascii.mp4", "size": "huge"}, 400),
    ({"id": "out:nope.mp4", "size": "small"}, 404),
])
def test_export_gif_bad_requests(env, body, status):
    client, _ = env
    response = client.post("/api/export/gif", json=body)
    assert response.status_code == status
    assert response.json()["error"]
