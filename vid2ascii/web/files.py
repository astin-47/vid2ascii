"""Maps opaque file ids to paths inside the library, uploads and outputs folders."""

import hashlib
import io
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from PIL import Image

from .. import InputError
from ..engine import VIDEO_EXTS
from ..frames import probe

MAX_UPLOAD_BYTES = 2 * 1024 ** 3
_SEP = "__"  # uploads are stored as <uuid>__<original name>


class FileStore:
    def __init__(self, library: Path, workdir: Path):
        self.library = Path(library)
        workdir = Path(workdir)
        self.uploads = workdir / "work" / "uploads"
        self.thumbs = workdir / "work" / "thumbs"
        self.outputs = workdir / "outputs"
        for folder in (self.uploads, self.thumbs, self.outputs):
            folder.mkdir(parents=True, exist_ok=True)
        self._roots = {"lib": self.library, "up": self.uploads, "out": self.outputs}
        self._probe_cache: dict = {}
        self._lock = threading.Lock()
        self._strip_locks: dict[str, threading.Lock] = {}

    # ---- ids ---------------------------------------------------------------
    def resolve(self, file_id: str) -> Path:
        """Return the file behind an id, or raise KeyError if it is not a known video."""
        kind, sep, rel = file_id.partition(":")
        root = self._roots.get(kind)
        if not sep or root is None or not rel:
            raise KeyError(file_id)
        if PureWindowsPath(rel).is_absolute() or PurePosixPath(rel).is_absolute() \
                or PureWindowsPath(rel).drive:
            raise KeyError(file_id)
        parts = re.split(r"[\\/]", rel)
        if any(part in ("", ".", "..") for part in parts):
            raise KeyError(file_id)
        path = root.joinpath(*parts)
        try:
            resolved = path.resolve()
            inside = resolved.is_relative_to(root.resolve())
        except OSError:
            raise KeyError(file_id) from None
        if not inside or not resolved.is_file() or resolved.suffix.lower() not in VIDEO_EXTS:
            raise KeyError(file_id)
        return resolved

    def id_for(self, path: Path) -> str:
        path = Path(path).resolve()
        for kind, root in self._roots.items():
            root = root.resolve()
            if path.is_relative_to(root):
                return f"{kind}:{path.relative_to(root).as_posix()}"
        raise KeyError(str(path))

    @staticmethod
    def display_name(path: Path) -> str:
        name = Path(path).name
        head, sep, tail = name.partition(_SEP)
        return tail if sep and len(head) == 32 else name

    # ---- metadata ----------------------------------------------------------
    def describe(self, path: Path) -> dict:
        path = Path(path)
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns)
        with self._lock:
            info = self._probe_cache.get(key)
        if info is None:
            info = probe(path)
            with self._lock:
                self._probe_cache[key] = info
        return {"id": self.id_for(path), "name": self.display_name(path), "size": stat.st_size,
                "duration": info.duration, "width": info.width, "height": info.height,
                "fps": info.fps, "has_audio": info.has_audio}

    def _list(self, folder: Path) -> list[dict]:
        items = []
        for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTS \
                    and ".partial." not in path.name:
                try:
                    items.append(self.describe(path))
                except (InputError, KeyError, OSError):
                    continue  # unreadable, or a link that leads outside the folder: skip it
        return items

    def list_library(self) -> list[dict]:
        return self._list(self.library)

    def list_uploads(self) -> list[dict]:
        return self._list(self.uploads)

    def list_outputs(self) -> list[dict]:
        items = self._list(self.outputs)
        for item in items:
            item["created"] = self.resolve(item["id"]).stat().st_mtime
        return sorted(items, key=lambda i: i["created"], reverse=True)

    # ---- writing -----------------------------------------------------------
    def new_upload_path(self, filename: str) -> Path:
        name = Path(filename or "upload.mp4").name
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name) or "upload.mp4"
        if Path(name).suffix.lower() not in VIDEO_EXTS:
            raise InputError(f"unsupported file type: {name}")
        return self.uploads / f"{uuid.uuid4().hex}{_SEP}{name}"

    def next_output_path(self, stem: str) -> Path:
        candidate = self.outputs / f"{stem}_ascii.mp4"
        n = 2
        while candidate.exists() or candidate.with_name(
                candidate.stem + ".partial" + candidate.suffix).exists():
            candidate = self.outputs / f"{stem}_ascii_{n}.mp4"
            n += 1
        return candidate

    def thumb(self, path: Path) -> Path:
        path = Path(path)
        digest = hashlib.sha1(f"{path}|{path.stat().st_mtime_ns}".encode()).hexdigest()
        target = self.thumbs / f"{digest}.jpg"
        if not target.exists():
            info = self.describe(path)
            at = max(0.0, (info["duration"] or 0) * 0.1)
            tmp = target.with_name(f"{digest}.{time.monotonic_ns()}.tmp.jpg")
            cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{at:.3f}",
                   "-protocol_whitelist", "file,pipe", "-i", str(path), "-frames:v", "1",
                   "-vf", "scale=320:-2", "-q:v", "4", str(tmp)]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                raise InputError(f"cannot read video: {self.display_name(path)}")
            tmp.replace(target)
        return target

    def strip(self, path: Path, frames: int = 10, height: int = 54) -> Path:
        """One JPEG of `frames` evenly spaced frames side by side, for the timeline.
        Each frame is a seek plus a single-frame decode, so a long video costs the same."""
        path = Path(path)
        stat = path.stat()
        digest = hashlib.sha1(
            f"strip|{path}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()
        target = self.thumbs / f"{digest}.jpg"
        with self._lock:
            lock = self._strip_locks.setdefault(digest, threading.Lock())
        with lock:  # concurrent requests for one strip build it once
            if not target.exists():
                self._build_strip(path, target, frames, height)
        return target

    def _build_strip(self, path: Path, target: Path, frames: int, height: int) -> None:
        duration = self.describe(path)["duration"]
        tiles = []
        for i in range(frames):
            at = duration * (i + 0.5) / frames if duration > 0 else 0.0
            cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{at:.3f}",
                   "-protocol_whitelist", "file,pipe", "-i", str(path), "-an",
                   "-frames:v", "1", "-vf", f"scale=-2:{height}", "-f", "image2pipe",
                   "-c:v", "png", "pipe:1"]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode == 0 and result.stdout:
                tiles.append(Image.open(io.BytesIO(result.stdout)).convert("RGB"))
        if not tiles:
            raise InputError(f"cannot read video: {self.display_name(path)}")
        while len(tiles) < frames:  # a seek past the last frame: repeat the last tile
            tiles.append(tiles[-1])
        width = tiles[0].width
        sheet = Image.new("RGB", (width * frames, height))
        for i, tile in enumerate(tiles):
            sheet.paste(tile.resize((width, height)), (i * width, 0))
        tmp = target.with_name(f"{target.stem}.{time.monotonic_ns()}.tmp.jpg")
        sheet.save(tmp, "JPEG", quality=80)
        tmp.replace(target)
