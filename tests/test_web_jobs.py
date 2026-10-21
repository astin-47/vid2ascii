import io
import json
import shutil
import socket
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from vid2ascii.engine import Options, plan
from vid2ascii.web.server import create_app, pick_port


@pytest.fixture(scope="session")
def clip_720p(tmp_path_factory):
    path = tmp_path_factory.mktemp("hd") / "hd.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def env(clip_169, clip_720p, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_169, lib / "a.mp4")
    shutil.copy(clip_720p, lib / "hd.mp4")
    app = create_app(lib, tmp_path / "work")
    with TestClient(app) as client:
        yield client, app, lib


def preview(client, file_id="lib:a.mp4", t=1.0, size="full", **options):
    return client.post("/api/preview",
                       json={"id": file_id, "t": t, "options": options, "size": size})


def events(client, job_id):
    """Collect (event, data) pairs from the SSE stream until it closes."""
    out, event = [], None
    with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                out.append((event, json.loads(line.split(":", 1)[1])))
    return out


def ffprobe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, check=True)
    return json.loads(result.stdout)


def test_preview_full_and_draft(env):
    client, _, lib = env
    full = preview(client)
    assert full.status_code == 200
    assert full.headers["content-type"] == "image/jpeg"
    assert int(full.headers["x-render-ms"]) >= 0
    assert full.headers["cache-control"] == "no-store"
    layout = plan(lib / "a.mp4", Options()).layout
    assert Image.open(io.BytesIO(full.content)).size == (layout.out_w, layout.out_h)
    draft = preview(client, size="draft")
    assert max(Image.open(io.BytesIO(draft.content)).size) <= 1100


def test_preview_bad_options_400(env):
    client, *_ = env
    response = preview(client, cols=0)
    assert response.status_code == 400
    assert response.json()["error"]


def test_preview_unknown_file_404(env):
    client, *_ = env
    assert preview(client, file_id="lib:../x.mp4").status_code == 404


def test_preview_latency_budget(env):
    client, *_ = env
    preview(client, "lib:hd.mp4", t=2.0)  # warm up
    setting = preview(client, "lib:hd.mp4", t=2.0, color="green")
    assert int(setting.headers["x-render-ms"]) <= 100
    t0 = time.perf_counter()
    preview(client, "lib:hd.mp4", t=6.5, size="draft")
    assert (time.perf_counter() - t0) * 1000 <= 300


def test_render_job_sse(env):
    client, app, _ = env
    job_id = client.post("/api/render", json={
        "id": "lib:a.mp4", "options": {"duration": 1, "cols": 40}}).json()["job_id"]
    stream = events(client, job_id)
    kinds = [kind for kind, _ in stream]
    assert "progress" in kinds
    assert kinds[-1] == "done"
    assert kinds.count("done") == 1
    output = app.state.files.resolve(stream[-1][1]["output_id"])
    probe = ffprobe(output)
    assert float(probe["format"]["duration"]) == pytest.approx(1, abs=0.15)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])


def test_render_bad_requests(env):
    client, *_ = env
    assert client.post("/api/render", json={"id": "lib:nope.mp4", "options": {}}
                       ).status_code == 404
    assert client.post("/api/render", json={"id": "lib:a.mp4", "options": {"fps": 0}}
                       ).status_code == 400


def test_cancel_job(env):
    client, app, _ = env
    job_id = client.post("/api/render", json={
        "id": "lib:hd.mp4", "options": {"duration": 3}}).json()["job_id"]
    out, event = [], None
    with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                if event == "progress" and not out:
                    client.post(f"/api/jobs/{job_id}/cancel")
                out.append(event)
    assert out[-1] == "cancelled"
    assert list(app.state.files.outputs.iterdir()) == []


def test_two_jobs_queue_fifo(env):
    client, app, _ = env
    body = {"id": "lib:a.mp4", "options": {"duration": 1, "cols": 40}}
    first = client.post("/api/render", json=body).json()["job_id"]
    second = client.post("/api/render", json=body).json()["job_id"]
    jobs = app.state.jobs
    overlap = False
    deadline = time.time() + 60
    while time.time() < deadline:
        a, b = jobs.get(first), jobs.get(second)
        if b.state == "running" and a.state not in ("done", "error", "cancelled"):
            overlap = True
        if a.state == "done" and b.state == "done":
            break
        time.sleep(0.02)
    assert not overlap
    names = sorted(app.state.files.resolve(jobs.get(j).output_id).name
                   for j in (first, second))
    assert names == ["a_ascii.mp4", "a_ascii_2.mp4"]


def test_preview_responsive_during_render(env):
    client, app, _ = env
    preview(client, "lib:hd.mp4", t=2.0)  # warm the cache
    job_id = client.post("/api/render", json={
        "id": "lib:hd.mp4", "options": {"duration": 3}}).json()["job_id"]
    deadline = time.time() + 30
    while app.state.jobs.get(job_id).state != "running" and time.time() < deadline:
        time.sleep(0.02)
    timings = []
    for color in ("green", "amber", "mono", "original", "green"):
        t0 = time.perf_counter()
        assert preview(client, "lib:hd.mp4", t=2.0, color=color).status_code == 200
        timings.append((time.perf_counter() - t0) * 1000)
    assert app.state.jobs.get(job_id).state == "running", "render finished too early"
    assert max(timings) <= 300, timings
    client.post(f"/api/jobs/{job_id}/cancel")


def test_serve_port_fallback():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert pick_port(port) == port + 1 or pick_port(port) > port
        assert pick_port(port) != port


def test_shutdown_cancels_running_job(clip_720p, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_720p, lib / "hd.mp4")
    app = create_app(lib, tmp_path / "work")
    with TestClient(app) as client:
        job_id = client.post("/api/render", json={
            "id": "lib:hd.mp4", "options": {"duration": 5}}).json()["job_id"]
        while app.state.jobs.get(job_id).state != "running":
            time.sleep(0.02)
    assert app.state.jobs.get(job_id).state == "cancelled"
    assert list((tmp_path / "work" / "outputs").iterdir()) == []


def test_cli_serve_dispatch(tmp_path, monkeypatch):
    import vid2ascii.web.server as server
    from vid2ascii.cli import main

    seen = {}

    def fake_serve(library, workdir, port, open_browser):
        seen.update(library=library, workdir=workdir, port=port, open_browser=open_browser)
        return 0

    monkeypatch.setattr(server, "serve", fake_serve)
    assert main(["serve", "--library", str(tmp_path), "--port", "9001", "--no-browser"]) == 0
    assert seen == {"library": tmp_path, "workdir": seen["workdir"], "port": 9001,
                    "open_browser": False}


def test_serve_no_free_port(monkeypatch, capsys):
    import vid2ascii.web.server as server

    monkeypatch.setattr(server, "pick_port", lambda start, tries=11: None)
    assert server.serve(server.Path("."), server.Path("."), 8765, False) == 2
    assert "no free port" in capsys.readouterr().err


def test_preview_reports_grid_and_output(env):
    client, _, lib = env
    layout = plan(lib / "a.mp4", Options(cols=80)).layout
    response = preview(client, size="draft", cols=80)
    assert response.headers["x-grid"] == f"{layout.cols}x{layout.rows}"
    assert response.headers["x-output"] == f"{layout.out_w}x{layout.out_h}"


def test_preview_renders_at_most_two_at_once(env, monkeypatch):
    import vid2ascii.web.server as server

    client, *_ = env
    active, peak = [0], [0]
    lock = threading.Lock()
    real = server.render_still

    def slow(*args, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(server, "render_still", slow)
    threads = [threading.Thread(target=preview, args=(client,), kwargs={"size": "draft"})
               for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert peak[0] <= 2, f"{peak[0]} previews rendered at once"
