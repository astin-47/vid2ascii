"""FastAPI app for the local web interface."""

import asyncio
import io
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile
import anyio
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import InputError, Vid2AsciiError
from ..atlas import CHARSETS
from ..engine import COLORS, MAX_DURATION, Options, layout_for, render_still
from ..frames import ASPECTS
from . import files as files_mod
from .files import FileStore
from .jobs import TERMINAL, JobQueue

STATIC = Path(__file__).parent / "static"
CHUNK = 1024 * 1024
SIZES = {"full": 1920, "draft": 960}
EVENT_INTERVAL = 0.25


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(library: Path, workdir: Path) -> FastAPI:
    store = FileStore(library, workdir)
    jobs = JobQueue(store)

    @asynccontextmanager
    async def lifespan(_):
        yield
        await run_in_threadpool(jobs.shutdown)

    app = FastAPI(title="vid2ascii", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.files = store
    app.state.jobs = jobs

    @app.exception_handler(ApiError)
    async def _api_error(_, exc: ApiError):
        return _error(exc.status, exc.message)

    @app.exception_handler(Vid2AsciiError)
    async def _domain_error(_, exc: Vid2AsciiError):
        return _error(400, str(exc))

    @app.exception_handler(ValueError)
    async def _value_error(_, exc: ValueError):
        return _error(400, str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_, exc: StarletteHTTPException):
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_, exc: Exception):
        return _error(500, f"unexpected error: {exc}")

    def lookup(file_id: str) -> Path:
        try:
            return store.resolve(file_id)
        except KeyError:
            raise ApiError(404, f"unknown file: {file_id}") from None

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/options")
    def options():
        return {
            "defaults": Options().to_dict(),
            "colors": list(COLORS),
            "charsets": CHARSETS,
            "aspects": ["source", *ASPECTS],
            "limits": {"max_duration": MAX_DURATION,
                       "max_upload_bytes": files_mod.MAX_UPLOAD_BYTES,
                       "cols": [20, 400]},
        }

    @app.get("/api/library")
    def library():
        result = {"items": [], "uploads": store.list_uploads(), "folder": str(store.library)}
        if not store.library.is_dir():
            result["warning"] = (f"library folder not found: {store.library} "
                                 "(start the server with --library PATH)")
            return result
        result["items"] = store.list_library()
        return result

    @app.post("/api/upload")
    async def upload(request: Request, file: UploadFile):
        limit = files_mod.MAX_UPLOAD_BYTES
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit + CHUNK:
            raise ApiError(413, "upload too large")
        try:
            target = store.new_upload_path(file.filename)
        except InputError as exc:
            raise ApiError(400, str(exc)) from None
        size = 0
        try:
            with open(target, "wb") as out:
                while chunk := await file.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise ApiError(413, "upload too large")
                    out.write(chunk)
            try:
                return store.describe(target)
            except InputError:
                raise ApiError(400, f"cannot read video: {store.display_name(target)}") \
                    from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    @app.get("/api/outputs")
    def outputs():
        return {"items": store.list_outputs()}

    @app.get("/api/files/{file_id:path}/thumb")
    def thumb(file_id: str):
        return FileResponse(store.thumb(lookup(file_id)), media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/files/{file_id:path}/strip")
    def strip(file_id: str):
        return FileResponse(store.strip(lookup(file_id)), media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/files/{file_id:path}/video")
    def video(file_id: str):
        return FileResponse(lookup(file_id))

    @app.get("/api/files/{file_id:path}/download")
    def download(file_id: str):
        path = lookup(file_id)
        name = store.display_name(path)
        disposition = f"attachment; filename*=UTF-8''{quote(name)}"
        return FileResponse(path, headers={"Content-Disposition": disposition})

    preview_slots = anyio.CapacityLimiter(2)  # previews render on at most two threads

    @app.post("/api/preview")
    async def preview(request: Request, body: dict):
        path = lookup(str(body.get("id", "")))
        opts = Options.from_dict(body.get("options") or {})
        long_side = SIZES.get(body.get("size", "full"), SIZES["full"])
        t = float(body.get("t") or 0.0)

        def work():
            started = time.perf_counter()
            frame = render_still(path, t, opts, long_side=long_side)
            buf = io.BytesIO()
            Image.fromarray(frame).save(buf, "JPEG", quality=85)
            elapsed = round((time.perf_counter() - started) * 1000)
            return buf.getvalue(), elapsed, layout_for(path, opts)

        async with preview_slots:
            if await request.is_disconnected():  # the page already asked for a newer frame
                return Response(status_code=204)
            data, elapsed, out = await anyio.to_thread.run_sync(work)
        return Response(data, media_type="image/jpeg", headers={
            "X-Render-Ms": str(elapsed), "Cache-Control": "no-store",
            "X-Grid": f"{out.cols}x{out.rows}", "X-Output": f"{out.out_w}x{out.out_h}",
            "Access-Control-Expose-Headers": "X-Render-Ms, X-Grid, X-Output"})

    def job_or_404(job_id: str):
        try:
            return jobs.get(job_id)
        except KeyError:
            raise ApiError(404, f"unknown job: {job_id}") from None

    @app.post("/api/render")
    def render(body: dict):
        file_id = str(body.get("id", ""))
        lookup(file_id)
        opts = Options.from_dict(body.get("options") or {})
        return {"job_id": jobs.submit(file_id, opts)}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        job_or_404(job_id)
        jobs.cancel(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str):
        job = job_or_404(job_id)

        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        async def stream():
            while job.state not in TERMINAL:
                yield sse("progress", job.snapshot())
                await asyncio.sleep(EVENT_INTERVAL)
            if job.state == "done":
                yield sse("done", {"output_id": job.output_id, **job.result})
            elif job.state == "error":
                yield sse("error", {"message": job.message})
            else:
                yield sse("cancelled", {})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def pick_port(start: int, tries: int = 11) -> int | None:
    """First port in [start, start + tries) that 127.0.0.1 can bind, else None."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return None


def serve(library: Path, workdir: Path, port: int, open_browser: bool) -> int:
    """Run the web interface on 127.0.0.1 until Ctrl+C. Returns a process exit code."""
    import sys
    import threading
    import webbrowser

    import uvicorn

    chosen = pick_port(port)
    if chosen is None:
        print(f"error: no free port in {port}-{port + 10}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{chosen}/"
    print(f"vid2ascii web interface: {url}  (library: {library})  Ctrl+C to stop")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(library, workdir), host="127.0.0.1", port=chosen,
                log_level="warning")
    return 0
