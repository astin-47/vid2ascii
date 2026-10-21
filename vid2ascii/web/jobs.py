"""Render job queue: one worker thread, FIFO, progress and cancel."""

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

from .. import Cancelled
from ..engine import Options, convert
from .files import FileStore

TERMINAL = ("done", "error", "cancelled")


@dataclass
class Job:
    id: str
    file_id: str
    options: Options
    state: str = "queued"
    done: int = 0
    total: int = 0
    fps: float = 0.0
    eta_s: float | None = None
    output_id: str | None = None
    result: dict | None = None
    message: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        return {"state": self.state, "done": self.done, "total": self.total,
                "fps": round(self.fps, 1),
                "eta_s": None if self.eta_s is None else round(self.eta_s, 1)}


class JobQueue:
    def __init__(self, files: FileStore):
        self.files = files
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="vid2ascii-render",
                                        daemon=True)
        self._worker.start()

    def submit(self, file_id: str, opts: Options) -> str:
        self.files.resolve(file_id)  # KeyError for unknown ids
        opts.validate()
        job = Job(uuid.uuid4().hex[:12], file_id, opts)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job.id

    def get(self, job_id: str) -> Job:
        with self._lock:
            return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        job.cancel.set()
        if job.state == "queued":
            job.state = "cancelled"

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.state not in TERMINAL:
                self.cancel(job.id)
        self._queue.put(None)
        self._worker.join(timeout=30)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            if job.cancel.is_set():
                job.state = "cancelled"
                continue
            self._execute(job)

    def _execute(self, job: Job) -> None:
        job.state = "running"
        started = time.monotonic()

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total
            elapsed = time.monotonic() - started
            if elapsed > 0:
                rate = done / elapsed
                job.fps = rate if job.fps == 0 else 0.7 * job.fps + 0.3 * rate
                job.eta_s = (total - done) / job.fps if job.fps > 0 else None

        try:
            src = self.files.resolve(job.file_id)
            stem = self.files.display_name(src).rsplit(".", 1)[0]
            dst = self.files.next_output_path(stem)
            result = convert(src, dst, job.options, on_progress=progress, cancel=job.cancel)
        except Cancelled:
            job.state = "cancelled"
        except Exception as exc:  # report every failure to the page
            job.message = str(exc) or exc.__class__.__name__
            job.state = "error"
        else:
            job.output_id = self.files.id_for(result.path)
            job.result = {"width": result.width, "height": result.height,
                          "duration": round(result.duration, 3)}
            job.state = "done"
