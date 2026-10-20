"""Audio decoding through an ffmpeg pipe and beat detection."""

import subprocess

import numpy as np

from . import InputError


def load_audio(path, start: float, duration: float, sr: int = 22050) -> np.ndarray:
    """Decode a mono float32 segment of `path` at sample rate `sr`."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin",
           "-ss", str(start), "-t", str(duration), "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except OSError as e:
        raise InputError(f"cannot run ffmpeg to read audio: {e}") from e
    if proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip()
        raise InputError(f"cannot decode audio from {path}: {msg}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)


def detect_beats(samples, sr) -> np.ndarray:
    """Return beat times in seconds (onsets if fewer than 2 beats found)."""
    import librosa

    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0 or not np.any(samples):
        return np.zeros(0, dtype=float)
    _, beats = librosa.beat.beat_track(y=samples, sr=sr, units="time")
    beats = np.atleast_1d(np.asarray(beats, dtype=float))
    if len(beats) < 2:
        beats = np.atleast_1d(np.asarray(
            librosa.onset.onset_detect(y=samples, sr=sr, units="time"), dtype=float))
    return beats


def beat_envelope(beat_times, n_frames: int, fps: float, decay: float = 0.15) -> np.ndarray:
    """Per-frame envelope: 1 on a beat, exp decay afterwards, 0 before the first."""
    beats = np.sort(np.asarray(beat_times, dtype=float))
    env = np.zeros(n_frames, dtype=np.float32)
    if beats.size == 0 or n_frames <= 0:
        return env
    t = np.arange(n_frames) / fps
    idx = np.searchsorted(beats, t + 1e-9, side="right") - 1
    valid = idx >= 0
    env[valid] = np.exp(-(t[valid] - beats[idx[valid]]) / decay)
    return env
