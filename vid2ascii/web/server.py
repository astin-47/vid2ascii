"""FastAPI app for the local web interface."""

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import InputError, Vid2AsciiError
from ..atlas import CHARSETS
from ..engine import COLORS, MAX_DURATION, Options
from ..frames import ASPECTS
from . import files as files_mod
from .files import FileStore

STATIC = Path(__file__).parent / "static"
CHUNK = 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(library: Path, workdir: Path) -> FastAPI:
    app = FastAPI(title="vid2ascii", docs_url=None, redoc_url=None)
    store = FileStore(library, workdir)
    app.state.files = store

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
        result = {"items": [], "uploads": store.list_uploads()}
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

    @app.get("/api/files/{file_id:path}/video")
    def video(file_id: str):
        return FileResponse(lookup(file_id))

    @app.get("/api/files/{file_id:path}/download")
    def download(file_id: str):
        path = lookup(file_id)
        name = store.display_name(path)
        disposition = f"attachment; filename*=UTF-8''{quote(name)}"
        return FileResponse(path, headers={"Content-Disposition": disposition})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
