# vid2ascii Web UI + Live Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local web page (`python -m vid2ascii serve`) to pick a video, tune settings against an instant ASCII preview, render with live progress, and play or download the result.

**Architecture:** The conversion pipeline moves from `cli.py` into `engine.py` (an `Options` dataclass, progress and cancel hooks, cached single-frame previews). A FastAPI app in `vid2ascii/web/` exposes files, previews and render jobs over JSON/SSE, and serves a no-build HTML/CSS/JS page designed with the taste skill.

**Tech Stack:** Python 3.13 in `.venv`, FastAPI, uvicorn, python-multipart, numpy, Pillow, ffmpeg. Tests use pytest + httpx (FastAPI `TestClient`). Frontend: vanilla ES modules.

**Spec:** `docs/superpowers/specs/2026-10-22-vid2ascii-web-ui-design.md` (builds on `docs/superpowers/specs/2026-10-20-vid2ascii-design.md`)

## Global Constraints

- Everything runs from the project `.venv`: install, tests (`.venv/Scripts/python -m pytest` on Windows), server, browser checks. Nothing goes into the global Python.
- The server binds to `127.0.0.1` only. Default port 8765; on conflict try the next 10 ports, then exit 2.
- Local files only: no URL fetching. Every file the API touches is addressed by an id (`lib:<rel posix path>`, `up:<uuid>.<ext>`, `out:<name>`) that resolves inside its root. Anything else returns 404.
- CLI behavior, flags, messages and exit codes stay unchanged. The existing tests pass with only import or monkeypatch-target path changes.
- Uploads go to `work/uploads/`, thumbnails to `work/thumbs/`, outputs to `outputs/`, all under the workdir (default: project root) and git-ignored. Upload max 2 GB.
- The output name is `outputs/<stem>_ascii.mp4`, then `_ascii_2.mp4`, `_ascii_3.mp4`… and never overwrites.
- Error JSON shape is `{"error": "<message>"}` with HTTP status 400 (bad options or invalid video), 404 (unknown id), 409 (output exists), 413 (upload too large), 500 (unexpected). `Vid2AsciiError` messages pass through verbatim.
- Preview budget, measured on this machine with a 720p source and default options:
  - setting change (cached frame): `X-Render-Ms` ≤ 100, total ≤ 150 ms;
  - new time: draft ≤ 300 ms, full ≤ 500 ms.
  - Draft uses `long_side=960`, full uses 1920, JPEG quality 85.
- Render jobs run one at a time in a FIFO queue on a worker thread. SSE `progress` is sent about every 250 ms.
- The UI is built with `taste-skill:design-taste-frontend` (app-screen mode), including its screenshot look-and-fix loop. No frontend build step and no framework.
- Commit messages end with a blank line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Uploading a file that is not a video** (e.g. a renamed .txt) should give 400 with the message, and no file left in uploads. Pinned in Task 3 (`test_upload_rejects_non_video`).
2. **Library entries with odd names** (spaces, unicode, `#`, `%`) should list, preview and stream correctly, because ids are URL-encoded. Pinned in Task 3 (`test_library_odd_names_roundtrip`).
3. **Rapid preview requests for one video while a render job runs** should keep answering within 2× budget and never deadlock. Pinned in Task 4 (`test_preview_responsive_during_render`).
4. **Starting a second render while one runs** should queue it (FIFO), and both finish with distinct output names. Pinned in Task 4 (`test_two_jobs_queue_fifo`).
5. **A source file replaced on disk under the same name** should not leave the preview serving a stale cached frame, because the cache key includes mtime. Pinned in Task 2 (`test_cache_invalidates_on_mtime`).

---

## File Structure

```
scripts/setup.ps1, scripts/setup.sh   create .venv + install deps (T0)
requirements.txt, requirements-dev.txt                             (T0)
vid2ascii/engine.py        Options, convert, write_previews        (T1)  render_still, PreviewCache (T2)
vid2ascii/frames.py        + decode_frame (single frame at t)      (T2)
vid2ascii/cli.py           thin wrapper over engine; `serve` subcommand (T1, T4)
vid2ascii/web/__init__.py
vid2ascii/web/files.py     FileStore: ids <-> paths, library, uploads, outputs, thumbs (T3)
vid2ascii/web/jobs.py      JobQueue: worker thread, progress, cancel (T4)
vid2ascii/web/server.py    create_app(library, workdir) routes; serve() (T3, T4)
vid2ascii/web/static/      index.html, app.css, app.js (T5)
tests/test_engine.py, tests/test_preview.py, tests/test_web_files.py, tests/test_web_jobs.py
```

Dependencies: T0 → T1 → T2. T3 needs only T0 and T1 (it uses `engine.VIDEO_EXTS` and `frames.probe`). T4 needs T2 and T3. T5 needs T4 (the API contract is fixed in the spec, but browser verification needs the server). T2 and T3 touch disjoint files and may run in parallel.

---

### Task 0: Virtualenv and dependencies

**Files:** Create `scripts/setup.ps1`, `scripts/setup.sh`, `requirements-dev.txt`. Modify `requirements.txt`, `.gitignore`, `README.md` (install section only).

- `requirements.txt` = numpy, Pillow, tqdm, librosa, fastapi, uvicorn, python-multipart. `requirements-dev.txt` = `-r requirements.txt`, pytest, httpx.
- The setup scripts create `.venv` if missing (`python -m venv .venv`), run `.venv` python `-m pip install --upgrade pip`, then `-m pip install -r requirements-dev.txt`. They are idempotent and print the activate and run commands at the end.
- `.gitignore` adds `.venv/`, `work/`, `outputs/`.
- README install section: run the setup script, then use `.venv\Scripts\python -m vid2ascii ...` (Windows) or `.venv/bin/python` elsewhere. Tests: `.venv\Scripts\python -m pytest`.

- [ ] Step 1: Write the files and run `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1`. Expected: `.venv` exists and `.venv/Scripts/python -c "import fastapi, uvicorn, multipart, httpx, librosa"` exits 0.
- [ ] Step 2: Run `.venv/Scripts/python -m pytest -q`. Expected: 117 passed (the existing suite, now inside the venv).
- [ ] Step 3: Run setup.ps1 a second time. Expected: no errors and nothing reinstalled from scratch.
- [ ] Step 4: Commit `chore: project virtualenv setup and web dependencies`.

---

### Task 1: Engine extraction

**Files:** Create `vid2ascii/engine.py`, `tests/test_engine.py`. Modify `vid2ascii/cli.py`, `vid2ascii/__init__.py`, `tests/test_cli.py` (monkeypatch targets only).

**Interfaces – Produces:**
- `vid2ascii.Cancelled(Vid2AsciiError)`.
- `engine.MAX_DURATION = 60.0` and `engine.VIDEO_EXTS` (moved from cli; cli re-imports them).
- `@dataclass(frozen=True) Options(start=0.0, duration=None, fps=24.0, cols=120, color="original", charset="standard", contrast="auto", beat=False, aspect=None, font=None)`:
  - `validate() -> None` raises `ValueError` for fps ≤ 0, cols < 1, start < 0, duration ≤ 0, a color not in `COLORS = ("original","green","amber","mono")`, a charset resolving to < 2 chars, or a contrast that `AutoContrast` rejects.
  - `Options.from_dict(d: dict) -> Options` ignores unknown keys and coerces numbers from JSON.
- `@dataclass ConvertResult(path: Path, width: int, height: int, duration: float, frames: int)`.
- `@dataclass Plan(info, start, dur, fps, n_frames, layout, chars, font)` plus `plan(src: Path, opts: Options, long_side: int = 1920, info: VideoInfo | None = None) -> Plan` (it probes only when `info` is None; T2 passes the cached info). This covers today's probe, duration clamping, layout and font steps from `cli.convert`, with the same errors and messages. T2 reuses it.
- `convert(src: Path, dst: Path, opts: Options, *, overwrite=False, on_progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None) -> ConvertResult`. This is the old `cli.convert` minus preview mode and printing:
  - `on_progress(done, total)` is called after each encoded frame;
  - when `cancel.is_set()` is checked between frames, the run aborts like the existing failure path (kill ffmpeg, remove the `.partial`) and raises `Cancelled("cancelled")`;
  - the tqdm bar stays only in the CLI path. The CLI passes an `on_progress` that drives tqdm. The engine has no tqdm import.
- `write_previews(src: Path, out_base: Path, opts: Options, count: int) -> list[Path]`: the old preview mode. It returns the written paths and raises `Vid2AsciiError` when 0 are written.
- `cli.convert(input_path, output_path, args) -> Path`: kept as a wrapper (args → Options → engine), and keeps its print lines byte-for-byte.

- [ ] Step 1: Write `tests/test_engine.py`:
  - `test_options_validate_ok` (defaults pass).
  - `test_options_validate_errors`, parametrized over `{"fps":0}`, `{"cols":0}`, `{"start":-1}`, `{"duration":0}`, `{"color":"pink"}`, `{"charset":"x"}`, `{"contrast":"nan"}`. Each raises ValueError.
  - `test_options_from_dict` coerces `{"cols":"80","fps":"30","beat":true,"zzz":1}` to cols 80, fps 30.0, beat True.
  - `test_convert_progress` (clip_169, `Options(duration=1, cols=40)`): on_progress is called, its last call is `(n, n)` with n == result.frames, and the output exists.
  - `test_convert_cancel` sets the event after 3 progress calls → `Cancelled`, and `list(tmp_path.iterdir()) == []`.
  - `test_write_previews_returns_paths` (count=2) → 2 existing PNGs.
- [ ] Step 2: Run `.venv/Scripts/python -m pytest tests/test_engine.py -v`. Expected: FAIL (ImportError).
- [ ] Step 3: Implement engine.py and slim cli.py. Change only the `monkeypatch.setattr("vid2ascii.cli.…")` targets in tests/test_cli.py to `vid2ascii.engine.…` where the patched name moved.
- [ ] Step 4: Run `.venv/Scripts/python -m pytest -q`. Expected: all pass (117 old + the new ones), with `git diff tests/test_cli.py` showing only target-string changes.
- [ ] Step 5: Commit `refactor: extract conversion engine with options, progress and cancel`.

---

### Task 2: Single-frame preview with cache

**Files:** Modify `vid2ascii/frames.py`, `vid2ascii/engine.py`. Create `tests/test_preview.py`.

**Interfaces – Consumes:** `engine.plan`, `Options`, `frames.Layout`, `crop_filter`, `probe`. **Produces:**
- `frames.decode_frame(path, info: VideoInfo, t: float, layout: Layout, aspect: float) -> np.ndarray`:
  - returns uint8 `(rows, cols, 3)`;
  - the command is `-ss t` before `-i`, then `-frames:v 1` and the same crop/scale/format filter chain as `iter_frames` without `fps`;
  - it uses `-protocol_whitelist file,pipe`;
  - if `t` is beyond the end, it retries at `max(0, info.duration - 0.1)`, and if still nothing comes back raises `InputError`.
- `engine.PreviewCache(max_frames=64, max_atlases=16)` with three cached lookups:
  - `info(src) -> VideoInfo`, keyed by `(str(src), mtime)`;
  - `frame(src, info, t, fps, layout, aspect)`, keyed by `(str(src), mtime, round(t*fps), layout.cols, layout.rows, round(aspect, 4))`;
  - `atlas(font, chars, cw, ch)`.
  - It is a thread-safe LRU (`OrderedDict` + `threading.Lock`).
- `engine.render_still(src: Path, t: float, opts: Options, *, long_side: int = 1920, cache: PreviewCache | None = None) -> np.ndarray`:
  - returns uint8 `(H, W, 3)` with no beat effect;
  - uses a module-level default cache when `cache` is None;
  - `t` is clamped to `[0, duration]`;
  - it builds a fresh `Renderer` per call, because Renderer is cheap and AutoContrast must not carry its EMA across stills.

- [ ] Step 1: Write `tests/test_preview.py`:
  - `test_decode_frame_shape` (clip_169, 40x12 layout) → (12, 40, 3) uint8.
  - `test_render_still_shape`: the default Options shape equals `(plan.layout.out_h, plan.layout.out_w, 3)`; with `long_side=960` the long side ≤ 1100.
  - `test_cache_reuses_frame_on_color_change`: spy on `vid2ascii.engine.decode_frame` (monkeypatch a counting wrapper), call render_still twice with color original then green at the same t → 1 decode.
  - `test_cache_invalidates_on_mtime`: copy the clip to tmp, render_still, overwrite the file with clip_916 (and bump mtime via `os.utime`), render_still → 2 decodes and a different shape.
  - `test_still_latency_budget`: build a 1280x720 10 s testsrc2 clip in tmp. Warm up with one call. A color-change call takes ≤ 100 ms (`time.perf_counter`). A new-t call with long_side 960 takes ≤ 300 ms.
- [ ] Step 2: Run `.venv/Scripts/python -m pytest tests/test_preview.py -v`. Expected: FAIL (ImportError).
- [ ] Step 3: Implement it. If the latency test fails, use superpowers:systematic-debugging (measure decode vs atlas vs render vs tint) before changing anything.
- [ ] Step 4: Run the full suite. Expected: all pass.
- [ ] Step 5: Commit `feat: cached single-frame preview rendering`.

---

### Task 3: Web app — files, library, uploads, streaming

**Files:** Create `vid2ascii/web/__init__.py`, `vid2ascii/web/files.py`, `vid2ascii/web/server.py`, `tests/test_web_files.py`.

**Interfaces – Consumes:** `frames.probe`, `engine.VIDEO_EXTS`, `vid2ascii.InputError`. **Produces:**
- `files.FileStore(library: Path, workdir: Path)` with:
  - `.uploads`, `.thumbs`, `.outputs` dirs (created on init);
  - `resolve(file_id: str) -> Path` (raises `KeyError` → 404). It uses `Path.resolve()` + `is_relative_to(root)`, and rejects absolute or `..` parts before resolving;
  - `id_for(path) -> str`;
  - `describe(path) -> dict` returning `{id, name, size, duration, width, height, fps, has_audio}`, probe-cached by `(path, mtime)`;
  - `list_library() -> list[dict]`;
  - `list_outputs() -> list[dict]` (adds `created`, newest first);
  - `next_output_path(stem) -> Path`;
  - `thumb(path) -> Path`: ffmpeg grabs the frame at 10 % of duration, `scale=320:-2`, JPEG, cached as `thumbs/<sha1(path+mtime)>.jpg`.
- `server.create_app(library: Path, workdir: Path) -> FastAPI`, with `app.state.files: FileStore`. Routes for this task:
  - `GET /` (index.html, which can be a placeholder until T5) and `/static`;
  - `GET /api/options` → `{defaults: Options-as-dict, colors, charsets: list(CHARSETS), aspects: ["source", *ASPECTS], limits: {max_duration: 60, max_upload_bytes: 2*1024**3, cols: [20, 400]}}`;
  - `GET /api/library` → `{items, warning?}` (missing library → `items: []` + warning);
  - `POST /api/upload`: streams to `uploads/up_<uuid><ext>` in 1 MB chunks, aborts with 413 past 2 GB (deleting the file), probes it, and on failure deletes and returns 400;
  - `GET /api/files/{id:path}/thumb`;
  - `GET /api/files/{id:path}/video` with Range → 206. Use Starlette `FileResponse` if the installed version honors Range (verify in the test); otherwise implement single-range parsing;
  - `GET /api/files/{id:path}/download` with an attachment `Content-Disposition` (filename* UTF-8);
  - `GET /api/outputs`;
  - error handlers mapping to the Global Constraints JSON shape.

- [ ] Step 1: Write `tests/test_web_files.py` with a fixture app = `create_app(tmp/lib, tmp/work)` and `TestClient`; the lib holds clip_169 copied as `a.mp4` and as `my clip ü #1%.mp4`:
  - `test_options_shape`: keys present and `defaults.cols == 120`.
  - `test_library_lists_videos`: 2 items with duration ≈ 3 and width 640, and a `notes.txt` in lib is not listed.
  - `test_library_missing_folder`: `items == []` and a warning present.
  - `test_library_odd_names_roundtrip`: the id of the ü file, URL-encoded, gives thumb 200 `image/jpeg` and video 200.
  - `test_path_escape_404`, parametrized over ids `lib:../secret.mp4`, `lib:C:/Windows/win.ini`, `lib:/etc/passwd`, `up:../../x.mp4`, `out:..\\x.mp4`, `zzz:a.mp4`. Each gives 404.
  - `test_upload_ok`: posting clip_169 as multipart gives 200 with an id starting `up:`, and the file exists.
  - `test_upload_rejects_non_video`: a txt renamed `.mp4` gives 400 with `"cannot read video"` in the error, and `uploads` is empty.
  - `test_upload_too_large`: monkeypatch the limit to 1000 bytes → 413 and uploads empty.
  - `test_video_range`: header `Range: bytes=0-99` gives 206, 100 bytes and `Content-Range` present.
  - `test_download_headers`: attachment present.
  - `test_next_output_path_never_overwrites`: create `x_ascii.mp4` → returns `x_ascii_2.mp4`.
- [ ] Step 2: Run them. Expected: FAIL.
- [ ] Step 3: Implement.
- [ ] Step 4: Run the full suite. Expected: all pass.
- [ ] Step 5: Commit `feat: web file store and media endpoints`.

---

### Task 4: Web app — preview, render jobs, serve command

**Files:** Create `vid2ascii/web/jobs.py`, `tests/test_web_jobs.py`. Modify `vid2ascii/web/server.py`, `vid2ascii/cli.py`.

**Interfaces – Consumes:** `engine.render_still`, `engine.convert`, `Options.from_dict/validate`, `Cancelled`, `FileStore`. **Produces:**
- `POST /api/preview` body `{id, t, options, size}`:
  - runs `render_still` in a `ThreadPoolExecutor(max_workers=2)` via `run_in_threadpool` / `anyio.to_thread`;
  - returns JPEG q85 with headers `X-Render-Ms` (int) and `Cache-Control: no-store`;
  - invalid options → 400.
- `jobs.JobQueue(files: FileStore)` with `submit(file_id, opts) -> str`, `get(job_id) -> Job`, `cancel(job_id)`, `shutdown()` (cancel all, join the worker).
  - `Job` fields: `id`, `state` (queued|running|done|error|cancelled), `done`, `total`, `fps`, `eta_s`, `output_id`, `result` (width/height/duration), `message`, `cancel: threading.Event`.
  - A single worker thread consumes a `queue.Queue` and calls `engine.convert(src, files.next_output_path(stem), opts, on_progress=..., cancel=job.cancel)`.
  - fps is computed as an EMA of the frame rate, and `eta_s = (total - done) / fps`.
- `POST /api/render` → `{job_id}` (400 on bad options, 404 on a bad id).
- `GET /api/jobs/{id}/events`: a `text/event-stream` `StreamingResponse` that sends `event: progress` with `data: {done,total,fps,eta_s,state}` every 250 ms while queued or running, then exactly one terminal event, `done` with `{output_id, width, height, duration}`, `error` with `{message}`, or `cancelled`, and closes.
- `POST /api/jobs/{id}/cancel` → `{ok: true}`.
- The FastAPI lifespan calls `jobs.shutdown()` on exit.
- `cli.main` handles `serve` when `argv[0] == "serve"`, using its own parser: `--library` (default `Path.home() / "Videos"`), `--workdir` (default cwd), `--port 8765`, `--no-browser`. It runs `require_ffmpeg()`, picks the first free port in 8765–8775 (else prints an error and returns 2), opens the browser via `webbrowser.open` on a `threading.Timer(1.0)` unless `--no-browser`, then calls `uvicorn.run(app, host="127.0.0.1", port=port)`. A file literally named `serve` is still reachable as `./serve` (note this in `--help`).

- [ ] Step 1: Write `tests/test_web_jobs.py` (same app fixture as T3; clip via the library):
  - `test_preview_full_and_draft`: the full JPEG decodes with PIL to the layout size, the draft long side ≤ 1100, and `X-Render-Ms` is present.
  - `test_preview_bad_options_400`: cols 0 → 400 with an error message.
  - `test_preview_latency_budget`: on a 720p 10 s clip, after a warm-up, a color change gives `X-Render-Ms` ≤ 100, and a new t with a draft gives wall time ≤ 300 ms.
  - `test_render_job_sse`: submit with duration 1 and cols 40, read the events stream until terminal. There is at least one `progress`, then `done`, and ffprobe on the resolved output_id shows duration ≈ 1 and an audio stream.
  - `test_cancel_job`: submit with duration 3, cancel after the first progress → terminal `cancelled` and outputs empty (no `.partial`).
  - `test_two_jobs_queue_fifo`: submit 2 jobs on the same file → both `done`, their output names differ (`_ascii.mp4`, `_ascii_2.mp4`), and job 2 never reports `running` before job 1 is terminal.
  - `test_preview_responsive_during_render`: while a 3 s render runs, 5 preview calls each finish ≤ 2 × the budget.
  - `test_serve_port_fallback`: occupy 8765 with a socket, call the port-picker helper `pick_port(8765)` → 8766.
- [ ] Step 2: Run them. Expected: FAIL.
- [ ] Step 3: Implement.
- [ ] Step 4: Run the full suite. Expected: all pass. Then run the manual smoke test `.venv/Scripts/python -m vid2ascii serve --no-browser --library <tmp lib>` and `curl -s 127.0.0.1:8765/api/options`. Expected: JSON.
- [ ] Step 5: Commit `feat: preview and render job API with serve command`.

---

### Task 5: The page (taste skill)

**Files:** Create `vid2ascii/web/static/index.html`, `app.css`, `app.js`. Modify `README.md` (a "Web interface" section).

**Interfaces – Consumes:** the T3/T4 HTTP API exactly as listed in the spec table. The page never hard-codes choices; it reads `/api/options`.

Requirements (from the spec; the taste skill decides the visuals):
- **Layout:** source panel (drop zone, click-to-browse, library list with thumb, name, duration and resolution), center preview stage (fixed aspect-ratio box, scrubber, start/end range clamped to 60 s), settings panel (cols slider+number, color segmented control, charset presets + custom field, contrast auto/none/factor, aspect presets + custom WxH, fps, beat), render bar (Render, progress with fps and ETA, Cancel, inline `<video controls>`, Download, recent renders).
- **States:** empty, loading, error toast showing the server message, Render disabled while options are invalid.
- **No-lag client rules:**
  - an `AbortController` on every preview request, with at most one in flight and the latest pending request replacing any queued one;
  - scrubber `input` → draft, `change`/release → full;
  - settings → full after a 200 ms debounce;
  - text inputs debounced 200 ms;
  - swap the image only after `img.decode()` (previous image stays visible);
  - a fixed aspect box (no layout shift).
- **Keyboard:** Space toggles result playback; ←/→ moves 1 frame, Shift+←/→ moves 1 s.
- Works from 1024 px wide upward.

- [ ] Step 1: Invoke `taste-skill:design-taste-frontend` (app-screen mode) and follow its process to produce the three static files against the live server (`.venv/Scripts/python -m vid2ascii serve --no-browser --library <tmp lib with synthetic clips>`). Use its real-browser screenshot look-and-fix loop.
- [ ] Step 2: Do a functional check in the built-in browser, covering empty state → select a library clip → scrub (preview updates, no blank flash) → change cols/color/charset/aspect → Render → progress → play → Download → recent renders list. Drag-and-drop upload is checked via the click-to-browse path. Screenshot each state.
- [ ] Step 3: Check responsiveness. In the browser, run a scripted loop: 20 rapid `input` events on the scrubber and 10 setting changes. Expected: no console errors, the final image matches the final settings, and the network panel shows aborted stale requests and no request pile-up.
- [ ] Step 4: Write the README "Web interface" section: setup, `serve` and its flags, and a screenshot saved in `docs/proof/web-ui.png`.
- [ ] Step 5: Run the full suite. Expected: all pass. Commit `feat: web interface`.
