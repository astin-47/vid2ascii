# vid2ascii

vid2ascii converts a local video into an ASCII-character video. Every frame is
reduced to a grid of characters that is drawn with a monospace font, and the
original audio is carried over to the result. Output is an H.264 MP4 with AAC
audio.

## Requirements

- Python 3.10 or newer
- `ffmpeg` and `ffprobe` on your `PATH`

Install ffmpeg:

```bash
winget install Gyan.FFmpeg
```

```bash
brew install ffmpeg
```

```bash
sudo apt install ffmpeg
```

(The commands are for Windows, macOS and Debian/Ubuntu respectively.)

## Install

Everything runs from a project virtualenv in `.venv`. The setup script creates it
and installs the dependencies (safe to re-run):

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

```bash
bash scripts/setup.sh
```

(Windows and macOS/Linux respectively.) Then run everything with the venv's
Python: `.venv\Scripts\python` on Windows, `.venv/bin/python` elsewhere. The
examples below write `python` for short.

## Quick start

```bash
python -m vid2ascii clip.mp4
```

This writes `clip_ascii.mp4` next to the input. Use `-o` to choose another
path. An existing output is never replaced unless you pass `--overwrite`.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `input` | required | Video file, or a folder of videos |
| `-o`, `--output` | `<name>_ascii.mp4` beside the input | Output file (folder mode: output folder, default `ascii_out` inside the input folder) |
| `--start` | `0` | Start time in seconds |
| `--duration` | whole clip, max 60 | Seconds to convert |
| `--fps` | `24` | Output frame rate |
| `--cols` | `120` | Number of character columns |
| `--color` | `original` | `original`, `green`, `amber` or `mono` |
| `--charset` | `standard` | `standard`, `detailed`, `blocks`, `binary`, or your own characters (dark to light) |
| `--contrast` | `auto` | `auto`, `none`, or a numeric factor |
| `--beat` | off | Flash on detected beats (needs audio) |
| `--preview N` | off | Write N preview PNGs instead of a video |
| `--aspect` | matches the input | `source`, `reel`, `square`, `wide`, `cinematic`, or `WxH` (center crop) |
| `--font` | auto-detected | Path to a monospace TTF/TTC font |
| `--overwrite` | off | Replace an existing output |

## Examples

Basic conversion with a chosen output name:

```bash
python -m vid2ascii clip.mp4 -o out.mp4
```

Convert a section (10 seconds starting at 0:30):

```bash
python -m vid2ascii clip.mp4 --start 30 --duration 10
```

Vertical reel (9:16), cropped from the center:

```bash
python -m vid2ascii clip.mp4 --aspect reel --cols 80
```

Cinematic widescreen (2.39:1):

```bash
python -m vid2ascii clip.mp4 --aspect cinematic
```

Green terminal look (use `--color amber` for amber):

```bash
python -m vid2ascii clip.mp4 --color green
```

Custom charset, ordered from dark to light:

```bash
python -m vid2ascii clip.mp4 --charset " .oO@"
```

Block characters instead of the default ramp:

```bash
python -m vid2ascii clip.mp4 --charset blocks
```

Flash on every detected beat:

```bash
python -m vid2ascii music_video.mp4 --beat
```

Write 4 preview PNGs to check the look quickly (no video is encoded):

```bash
python -m vid2ascii clip.mp4 --preview 4
```

Convert every video in a folder (`.mp4 .mov .mkv .avi .webm .m4v`) into
`<name>_ascii.mp4` files in the output folder:

```bash
python -m vid2ascii my_videos -o ascii_videos
```

Use a specific font:

```bash
python -m vid2ascii clip.mp4 --font C:/Windows/Fonts/consola.ttf
```

## Output size and aspect

The long side of the output is about 1920 px (it varies a little with `--cols`
because character cells are whole pixels). By default the output has the same
aspect ratio as the input. With `--aspect` the source is center-cropped to fill
the chosen shape (`reel` 9:16, `square` 1:1, `wide` 16:9, `cinematic` 2.39:1,
or any `WxH` such as `4x5`) rather than letterboxed. `--cols` sets how many
character columns span the width, and the number of rows is derived from the
font's cell proportions, so characters (which are taller than wide) keep the
picture undistorted.

## How it works

1. `ffmpeg` decodes the video (cropped and scaled to the character grid) into a
   raw pipe.
2. Each frame arrives as a small numpy array with one pixel per character cell.
3. Brightness picks a glyph from a pre-rendered atlas of the charset in the
   chosen font, and the glyphs are tiled into a full-size frame (tinted by the
   source color or by the `--color` palette).
4. The rendered frames are piped straight into a second `ffmpeg`, which encodes
   H.264 and muxes the original audio from the input.

No temporary frame files are written.

## Running tests

```bash
.venv/Scripts/python -m pytest
```

(On macOS/Linux: `.venv/bin/python -m pytest`.)

## Notes

- Local video files only.
- Each clip is limited to 60 seconds; longer inputs are truncated to 60 s from
  `--start`.
- Errors print `error: ...` to stderr and exit with code 2. In folder mode a
  failed file is reported, the rest continue, and the exit code is 1.
- If the input has no audio the output is silent, and `--beat` is ignored with a
  warning.
