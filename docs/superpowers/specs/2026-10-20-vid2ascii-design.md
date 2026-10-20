# vid2ascii — Design Spec

Date: 2026-10-20
Status: Approved in chat, pending written-spec review

## Purpose

A reusable Python CLI that turns any local video into an ASCII/text-character
video, keeping the original audio:

    python -m vid2ascii input.mp4 -o output.mp4 [options]

Only local files are processed. The tool never downloads anything.

## User decisions (from brainstorming)

- Output aspect **matches the input video by default**. The user can override
  it with `--aspect reel|square|wide|cinematic|WxH`.
- When the chosen aspect differs from the input, the source is **center-cropped
  to fill** (no black bars).

## Architecture

```
input file ──ffprobe──> VideoInfo (w, h, fps, duration, has_audio)
      │
      ├─ ffmpeg decode: -ss start -t dur, fps=F, crop=<aspect>, scale=cols:rows:flags=area
      │      └─ rgb24 pipe → numpy (rows, cols, 3) per frame
      │
      ├─ [--beat] ffmpeg decode audio → mono float32 pipe → librosa beat_track → beat times
      │
      ├─ render.py: luminance → contrast → ramp index (+beat shift/swap)
      │      → atlas[idx] → (rows*ch, cols*cw) mask → tint → uint8 RGB frame
      │
      └─ ffmpeg encode: rawvideo stdin (input 0) + original file with same -ss/-t (input 1)
             -map 0:v -map 1:a? → libx264 yuv420p + aac → output.mp4
```

No temp frame folders. Every stage streams through pipes.

## Modules

### `vid2ascii/frames.py` — probing, layout, decode
- `probe(path) -> VideoInfo`: runs ffprobe (JSON) and returns width, height, fps,
  duration and has_audio. Raises `InputError` on an unreadable or non-video
  file.
- `parse_aspect(spec, src_w, src_h) -> float`: `None`/`"source"` gives the
  source ratio (w/h); presets are `reel`=9/16, `square`=1, `wide`=16/9,
  `cinematic`=2.39, plus `WxH` (e.g. `1080x1350`). Raises `ValueError` on bad
  input.
- `compute_layout(aspect, cols, cell_w, cell_h, long_side=1920) -> Layout`:
  - target W,H: the long side is 1920 and the other side is 1920/aspect (or
    1920*aspect).
  - The pixel cell width is `cw = max(4, round(W / cols))`, bumped by one if
    `cols` is odd and `cw` is odd (so `cols*cw` is even). The cell height is
    `ch = max(4, round(cw * font_cell_h / font_cell_w))`, which corrects for
    characters being taller than wide.
  - Rows come from the realized width: `rows = max(1, round(cols*cw / aspect / ch))`.
    If `rows*ch` is odd, `ch` is incremented by one and `rows` recomputed, so
    evenness is reached by minimal adjustment rather than rounding every cell up.
  - Output size = `cols*cw x rows*ch`, always even so libx264 accepts it, and
    within half a row of the requested aspect.
  - The CLI crops the source to the realized aspect `out_w / out_h`, not the
    requested one, so the picture is never stretched.
  - `Layout` holds cols, rows, cw, ch, out_w, out_h.
- `crop_filter(src_w, src_h, aspect) -> str`: a center-crop expression that
  produces the target aspect (an empty string when the aspect already matches
  within 1%).
- `iter_frames(path, start, duration, fps, layout, aspect) -> Iterator[ndarray]`:
  streams `(rows, cols, 3)` uint8 frames from the ffmpeg pipe.

### `vid2ascii/atlas.py` — glyph atlas
- `CHARSETS`: `standard` = `" .:-=+*#%@"`, `detailed` = a 70-character ramp,
  `blocks` = `" ░▒▓█"`, `binary` = `" 01"`. `resolve_charset(name_or_str)`
  returns the preset, or the string itself for a custom ramp (needs at least 2
  characters).
- `find_font(path=None) -> str`: tries the explicit path, then Consolas,
  Cascadia Mono, Courier New and DejaVu Sans Mono in the Windows, macOS and
  Linux font dirs. Raises `FontError` with a helpful message.
- `font_cell_ratio(font_path) -> (w, h)`: the cell size at a reference size.
- `build_atlas(chars, font_path, cw, ch) -> ndarray float32 (n, ch, cw)` in 0..1.
  Each glyph is rendered once with PIL and centered in its cell. The font size
  is picked so the glyph fits the cell.

### `vid2ascii/render.py` — pure numpy frame building
- `luminance(rgb) -> float32 (rows, cols)` in 0..1 (Rec.709 weights).
- `AutoContrast`: a stateful object that takes the 2nd and 98th percentile
  brightness of each frame and smooths them with an EMA (α=0.2). It then
  stretches the frame between them and applies a gamma lift when the mean is
  below 0.35. `--contrast none` does nothing; a number is a fixed gain around
  0.5.
- `map_to_indices(lum, n_chars) -> int (rows, cols)`: `floor(lum * n)`, clipped
  to the range 0..n-1. Index 0 is the darkest character.
- `compose(indices, atlas, layout) -> float32 mask (H, W)`, built with
  `atlas[idx]` and then transpose/reshape. There is no loop over cells.
- `tint(mask, rgb_cells, mode, lum) -> uint8 (H, W, 3)`:
  - `original`: each cell's color, normalized so the brightest channel is at
    least 0.35 (so dark colors still show), then upscaled with `np.repeat`.
  - `green` (#33ff66), `amber` (#ffb000), `mono` (white): the mask times that
    color.
- `apply_beat(lum, indices, strength, rng)`: when strength > 0, brightness is
  boosted by `0.35*s`, indices shift up by `round(s*n*0.3)`, and a `0.15*s`
  fraction of cells swap to random characters.
- `Renderer` combines all of the above. `render(rgb_cells, beat_strength=0)`
  returns the frame.

### `vid2ascii/audio.py`
- `load_audio(path, start, duration, sr=22050) -> ndarray`: ffmpeg decode to a
  mono f32le pipe.
- `detect_beats(samples, sr) -> ndarray[sec]`: `librosa.beat.beat_track` with
  `units="time"`, falling back to onset peaks when it finds fewer than 2
  beats.
- `beat_envelope(beat_times, n_frames, fps, decay=0.15) -> float32 (n_frames,)`:
  set to 1 at each beat and decaying as `exp(-dt/decay)`.

### `vid2ascii/cli.py` + `__main__.py`
Options: `input`, `-o/--output`, `--start` (0), `--duration` (whole clip, max
60), `--fps` (24), `--cols` (120), `--color` (original|green|amber|mono,
default original), `--charset` (standard), `--contrast` (auto), `--beat`,
`--preview N`, `--aspect`, `--font`, `--overwrite`.
- The duration is clamped to `min(60, video.duration - start)`. A `start` past
  the end of the video is an error.
- Default output: `<stem>_ascii.mp4` next to the input. In folder mode the
  default is `<folder>/ascii_out/`.
- Folder mode converts every `.mp4 .mov .mkv .avi .webm .m4v` file, skips
  failures, and prints a summary.
- `--preview N` writes N evenly spaced frames as `<output stem>_preview_01.png`
  and so on, then exits without encoding.
- The tqdm progress bar shows frames done.
- Errors (`Vid2AsciiError` subclasses: `FFmpegNotFound`, `InputError`,
  `FontError`) print `error: ...` and exit with code 2.

## Error handling
- `shutil.which("ffmpeg"/"ffprobe")` is checked up front, and the message
  includes install hints.
- Unreadable or non-video inputs: ffprobe fails, or no video stream is found,
  which raises `InputError` naming the file.
- An encoder failure surfaces the ffmpeg stderr tail.
- An existing output without `--overwrite` is an error.

## Performance target
A 10s clip at 24fps and 120 cols renders in about 30s or less. That means about
240 frames, each about 1920×1080. Indexing plus tint costs a few ms per frame;
x264 `-preset veryfast` encodes it.

## Testing (pytest)
- A session fixture generates synthetic clips with ffmpeg lavfi into tmp:
  - 16:9 `testsrc2` 640×360 + a 440Hz `sine`, 3s.
  - A 9:16 clip.
  - A clip without audio.
  - A click track: `aevalsrc` impulses at 120 BPM.
- Unit tests:
  - Ramp mapping: endpoints, monotonic.
  - `resolve_charset`.
  - Atlas shape, and that space is blank and `@` is denser than `.`.
  - `parse_aspect` and `compute_layout`: the ratio is preserved within the
    rounding tolerance, character aspect is corrected, and sizes are even.
  - Crop filter.
  - Auto-contrast lifts a dark frame.
  - Beat envelope peaks.
- Integration tests:
  - Output duration within ±0.15s, resolution as computed, and an audio
    stream present (ffprobe).
  - The no-audio input works.
  - `--aspect reel` gives a 9:16 output.
  - `--preview 3` writes 3 PNGs and no mp4.
  - Beat mode on the click track detects beats, and frames on beats differ
    from off-beat frames.
  - Folder mode.
  - Error exits for a bad file and a bad font.
- Final verification: render a synthetic clip, extract 2 frames with ffmpeg,
  inspect them visually, and show them to the user.

## Dependencies
`numpy`, `Pillow`, `tqdm`, `librosa` (only imported for `--beat`), and `pytest`
for dev. ffmpeg/ffprobe must be on PATH.
