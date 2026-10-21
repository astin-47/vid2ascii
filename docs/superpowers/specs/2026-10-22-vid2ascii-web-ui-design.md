# vid2ascii Web UI + Live Preview — Design Spec

Date: 2026-10-22
Status: Approved in chat, pending written-spec review
Builds on: `docs/superpowers/specs/2026-10-20-vid2ascii-design.md` (the CLI engine)

## Context and scope

This is sub-project 1 of 4 in the "improve vid2ascii" roadmap the user agreed:

1. **Web UI + live preview (this spec)**
2. Looks & effects (edge mode, CRT scanlines and glow, invert, palettes, matrix-rain beats)
3. Output options (GIF/WebM, quality slider, 30/60 fps, longer than 60 s)
4. Faster rendering (multi-core, NVENC)

Sub-projects 2–4 get their own spec → plan → build cycles later. They are out of scope here.

## User decisions (from brainstorming)

- The page is **for the user only and runs locally**. `python -m vid2ascii serve` starts a server on `127.0.0.1` and opens the browser. Nothing is hosted and there are no accounts.
- **Input:** both drag & drop (or click-to-browse) uploads **and** a library list of a local folder (default: the user's `Videos` folder, configurable).
- **Output:** the finished video plays in the page with a Download button, and is also saved to `outputs/` in the project.
- **The UI must look good**, and its visual design must be built with **`taste-skill:design-taste-frontend`**, including that skill's real-browser screenshot look-and-fix loop.
- **No lag** in the UI (see the performance budget below).
- **Everything runs in a project virtualenv `.venv`**: dependencies, server, tests and the README instructions. Nothing goes into the global Python.

## Architecture

```
browser (static HTML/CSS/JS, no build step)
   │  fetch JSON / multipart, <img> previews, EventSource progress, <video> playback
   ▼
vid2ascii/web/server.py  (FastAPI app, uvicorn, 127.0.0.1)
   │  thread pool for preview renders; single worker thread + FIFO queue for render jobs
   ▼
vid2ascii/engine.py  (new: pipeline extracted from cli.py)
   │
   ▼
frames.py / atlas.py / render.py / audio.py  (unchanged behavior)
```

### `vid2ascii/engine.py` (refactor — new module)
- `@dataclass(frozen=True) Options`: `start: float = 0.0`, `duration: float | None = None`, `fps: float = 24.0`, `cols: int = 120`, `color: str = "original"`, `charset: str = "standard"`, `contrast: str = "auto"`, `beat: bool = False`, `aspect: str | None = None`, `font: str | None = None`. `Options.validate()` raises `ValueError` using the same rules as the CLI today (fps > 0, cols ≥ 1, start ≥ 0, duration > 0, valid color, charset ≥ 2 chars, finite contrast).
- `convert(src: Path, dst: Path, opts: Options, *, overwrite: bool = False, on_progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None) -> ConvertResult`. This is today's `cli.convert` logic moved here: the partial-file safety, unknown-duration handling and 60 s cap all stay. `ConvertResult(path, width, height, duration, frames)`. If `cancel` is set, the encode stops, the partial file is removed, and `Cancelled` (a new `Vid2AsciiError` subclass) is raised.
- `write_previews(src, out_base: Path, opts, count) -> list[Path]`: today's `--preview` behavior.
- `render_still(src: Path, t: float, opts: Options, *, long_side: int = 1920) -> np.ndarray`: renders one frame at time `t` with no beat effect. It uses `PreviewCache` (below).
- `PreviewCache` (an in-process LRU with locks):
  - The decoded grid frame is keyed by `(src, mtime, t rounded to 1/fps, cols, rows, crop aspect)`, max 64 entries.
  - The glyph atlas is keyed by `(font, charset, cw, ch)`, max 16 entries.
  - The probe `VideoInfo` is keyed by `(src, mtime)`.
  - A change to color or contrast alone reuses the cached grid frame and atlas. Nothing new is decoded.
- `cli.py` becomes a thin wrapper: argparse → `Options` → `engine.convert` / `engine.write_previews`, plus folder mode and the error → exit-code mapping. **CLI behavior, flags, messages and exit codes stay unchanged. All 117 existing tests must pass, with tests changed only to update import paths.**

### `vid2ascii/web/server.py` (new)
`create_app(library: Path, workdir: Path) -> FastAPI`. The `serve` sub-command runs `uvicorn` on `127.0.0.1:<port>` (default 8765; on conflict it tries the next 10 ports) and opens the browser unless `--no-browser` is given.

Paths:
- `workdir` defaults to the project root.
- Uploads go to `workdir/work/uploads/`.
- Outputs go to `workdir/outputs/`.
- Both are git-ignored.

Every file the API touches is referenced by an opaque **file id** rather than a path:
- A library file's id is `lib:<relative posix path>`.
- An upload's id is `up:<uuid>.<ext>`.
- An output's id is `out:<name>`.

The server resolves each id to an absolute path and **rejects** any result that is not inside its root (library / uploads / outputs) with a 404. That rules out `..` escapes, absolute paths and symlinks pointing outside the root.

Endpoints (JSON unless noted):

| Method & path | Purpose |
|---|---|
| `GET /` | the page (`static/index.html`); `/static/*` for assets |
| `GET /api/options` | defaults and choices for every setting (colors, charsets, aspect presets, limits), so the UI never hard-codes them |
| `GET /api/library` | videos in the library folder (non-recursive, `VIDEO_EXTS`): `[{id, name, size, duration, width, height, fps, has_audio}]`. Probe results are cached per mtime. |
| `POST /api/upload` (multipart) | saves to uploads; max 2 GB; rejects non-video files after an ffprobe check; returns the same object shape as a library entry |
| `GET /api/files/{id}/thumb` | a small JPEG of the frame at 10 % of the duration, for list thumbnails, cached on disk under `work/thumbs/` |
| `POST /api/preview` | body `{id, t, options, size: "full"\|"draft"}` → `image/jpeg`. `draft` renders with `long_side=960`, `full` with 1920. JPEG quality 85. Header `X-Render-Ms`. |
| `POST /api/render` | body `{id, options}` → `{job_id}`. Jobs run one at a time in a FIFO queue. The output is `outputs/<stem>_ascii[_N].mp4` (it never overwrites). |
| `GET /api/jobs/{job_id}/events` | Server-Sent Events: `progress {done, total, fps, eta_s}` about every 250 ms, then a final `done {output_id, width, height, duration}`, `error {message}` or `cancelled` |
| `POST /api/jobs/{job_id}/cancel` | sets the job's cancel event |
| `GET /api/outputs` | recent renders, newest first: `[{id, name, size, created, width, height, duration}]` |
| `GET /api/files/{id}/video` | streams any file id with HTTP Range support, so `<video>` can seek |
| `GET /api/files/{id}/download` | the same file with `Content-Disposition: attachment` |

Errors use the shape `{"error": "<message>"}` with HTTP 400 (bad options or invalid video), 404 (unknown id), 409 (output exists), 413 (upload too large) or 500 (unexpected). `Vid2AsciiError` messages are passed through verbatim.

### `vid2ascii/web/static/` (new: `index.html`, `app.css`, `app.js`)
Plain HTML/CSS/ES modules, no framework and no build step. The design is produced with `taste-skill:design-taste-frontend` (app-screen mode). It must fit the product: a dark, terminal and monospace-inflected tool UI where the live ASCII preview is the hero. The taste skill decides the exact palette, type and details. The functional layout is fixed below.

Single-screen layout, desktop-first, usable down to 1024 px wide:
- **Source panel (left):** a drag-and-drop zone (also click-to-browse) and a Library list with thumbnail, name, duration and resolution. The selected item is highlighted.
- **Preview stage (center):** a large ASCII preview kept at the output aspect ratio. Under it:
  - a timeline scrubber showing the current time;
  - a range selector with start and end handles, which set `start`/`duration`. The range length is clamped to 60 s, and the UI shows it.
- **Settings panel (right):** cols (slider + number), color (segmented control), charset (presets + custom text field), contrast (auto / none / a factor slider), aspect (source / reel / square / wide / cinematic / custom WxH), fps, beat toggle. Every control updates the preview.
- **Render bar (bottom):** a Render button, a progress bar with fps and ETA, and Cancel. When the render finishes, the result plays inline (`<video controls>`) with Download. The Recent renders list sits underneath.
- **States:** empty (no video chosen), loading, error toasts that show the server's message, and a disabled Render button while the options are invalid.
- **Keyboard:** Space toggles result playback; ←/→ nudge the scrubber by 1 frame and Shift+←/→ by 1 s.

## Performance budget ("no lag")

Server side, measured on the user's machine with a 720p source and default options:
- **Setting change** (cached grid frame): `/api/preview` full in ≤ 150 ms, `X-Render-Ms` ≤ 100.
- **Scrub to a new time** (decode needed): draft ≤ 300 ms, full ≤ 500 ms.
- The decode for a new `t` spawns one ffmpeg with `-ss t` before `-i` and `-frames:v 1` at grid size.

Client side:
- Only the newest preview request matters: each new request aborts any in-flight one (`AbortController`). At most one runs at a time; the latest pending request replaces any queued one.
- **While dragging the scrubber**, request `draft` with throttling to one in flight. **On release** (or 200 ms after the last settings change), request `full`.
- Show the previous image until the new one has decoded (`img.decode()`) and then swap, so there is never a blank flash.
- Text inputs are debounced by 200 ms. Sliders update their label on `input` and send requests through the throttle.
- No layout shift: the preview stage has a fixed aspect-ratio box.
- Rendering never blocks the API, because preview and job work run in worker threads. During a render, previews still respond within about 2× budget.

## Environment

- `.venv` lives in the project root and is git-ignored.
- `scripts/setup.ps1` (Windows) and `scripts/setup.sh` (macOS/Linux) create `.venv`, upgrade pip, and install `requirements.txt` + `requirements-dev.txt`. Both are idempotent.
- `requirements.txt` gains `fastapi`, `uvicorn`, `python-multipart`. `requirements-dev.txt` holds `pytest`, `httpx`. Pytest moves out of `requirements.txt`.
- The README documents setup, `serve`, and running tests, all via `.venv` (`.venv\Scripts\python -m ...`).
- All verification in this sub-project (tests, server, browser checks) runs with the `.venv` interpreter.

## Error handling

- Upload of a non-video file: 400 `cannot read video: <name>`, and the file is deleted.
- Upload that is too large: 413.
- Library folder missing or unreadable: `/api/library` returns `[]` plus `{"warning": ...}`, and the UI shows a hint with the `--library` flag.
- A render error shows a toast, and no partial output is left behind (the engine guarantees this).
- If ffmpeg is missing at `serve` startup, it prints the existing `FFmpegNotFound` message and exits with code 2.
- Port in use: it tries the next 10 ports, then exits with code 2 and a message.
- On server shutdown (Ctrl+C), running jobs are cancelled and partial files removed.

## Testing

- **Engine refactor:** the existing 117 tests pass with only import changes. New unit tests:
  - `Options.validate`;
  - `render_still` output shape and dtype;
  - `PreviewCache`: the second call with a color-only change does not invoke the decoder (spy on `iter_frames` / the single-frame decode);
  - `convert` cancel → `Cancelled`, with no files left.
- **API (FastAPI `TestClient` / httpx; synthetic lavfi clips):**
  - `/api/options` shape.
  - Library listing with a temp library.
  - Path escape: `lib:../x`, an absolute path, and an `up:` id outside uploads all return 404.
  - Upload of a valid clip → 200 with metadata; upload of a text file → 400 and the file removed.
  - Preview full and draft → JPEG of the expected size.
  - Preview latency: after a warm-up call, a setting-change preview's `X-Render-Ms` ≤ 100, and the scrub-to-new-time draft's ≤ 300. The budget applies to this machine, so this is a plain assertion.
  - Render job: SSE yields progress then `done`, and the output plays (ffprobe duration and audio stream).
  - Cancel: SSE `cancelled`, no file left.
  - Download returns attachment headers; video Range request → 206.
- **Real-browser check (the controller does this before claiming completion):**
  - Run `serve` from `.venv` and open the page in the built-in browser.
  - Drag in or select a clip, scrub, change cols/color/charset/aspect, render, watch the progress, play the result, download.
  - Screenshot each state. This is in addition to the taste skill's own look-and-fix loop.

## Out of scope (this sub-project)

- New visual effects, new output formats, raising the 60 s cap, and performance work on full renders (sub-projects 2–4).
- Multi-user support, authentication, LAN or remote access, and hosting.
- Fetching videos from URLs (the local-files-only rule still holds).
