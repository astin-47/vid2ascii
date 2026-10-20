import numpy as np

from vid2ascii.audio import beat_envelope, detect_beats, load_audio


def test_envelope_peaks():
    env = beat_envelope(np.array([0.5]), 20, 10)
    assert env.dtype == np.float32
    assert env[5] == 1
    assert env[4] == 0
    assert env[6] < 1
    assert env[19] < 0.01


def test_load_audio_length(clip_169):
    y = load_audio(clip_169, 1.0, 1.0)
    assert y.dtype == np.float32
    assert abs(len(y) - 22050) <= 0.02 * 22050


def test_detect_beats_clicks(clip_clicks):
    y = load_audio(clip_clicks, 0.0, 4.0)
    beats = detect_beats(y, 22050)
    assert len(beats) >= 4
    assert abs(np.median(np.diff(beats)) - 0.5) <= 0.08
