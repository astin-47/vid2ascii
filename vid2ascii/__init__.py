"""vid2ascii: convert local videos to ASCII-art videos."""

__version__ = "0.1.0"


class Vid2AsciiError(Exception):
    """Base class for all vid2ascii errors."""


class FFmpegNotFound(Vid2AsciiError):
    """ffmpeg/ffprobe could not be found on PATH."""


class InputError(Vid2AsciiError):
    """Invalid or unreadable input."""


class FontError(Vid2AsciiError):
    """No usable monospace font could be loaded."""
