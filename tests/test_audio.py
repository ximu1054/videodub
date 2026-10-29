"""音频原语。裁静音那 0.3~0.8 秒是真实收益，不能悄悄失效。"""

from __future__ import annotations

import numpy as np
import pytest

from videodub.media.audio import (SR, fade, peak_normalize, read_wav_16k, trim_silence,
                                  write_wav16)


def tone(sec: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(sec * SR), dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(sec: float) -> np.ndarray:
    return np.zeros(int(sec * SR), dtype=np.float32)


def test_trim_removes_both_ends():
    audio = np.concatenate([silence(0.3), tone(1.0), silence(0.4)])
    out = trim_silence(audio)
    assert len(out) / SR == pytest.approx(1.0 + 2 * 0.03, abs=0.02)


def test_trim_keeps_short_pad():
    audio = np.concatenate([silence(0.5), tone(0.5), silence(0.5)])
    out = trim_silence(audio, keep_ms=30)
    assert len(out) / SR < 0.6


def test_trim_all_silence_returns_input():
    audio = silence(1.0)
    assert len(trim_silence(audio)) == len(audio)


def test_trim_empty():
    assert trim_silence(np.array([], dtype=np.float32)).size == 0


def test_trim_does_not_change_content():
    audio = np.concatenate([silence(0.2), tone(0.5), silence(0.2)])
    out = trim_silence(audio)
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=1e-3)


def test_fade_reaches_zero_at_edges():
    audio = np.ones(SR // 2, dtype=np.float32)
    out = fade(audio, ms=40)
    assert abs(out[0]) < 1e-6
    assert abs(out[-1]) < 1e-6
    assert out[len(out) // 2] == pytest.approx(1.0)


def test_fade_on_short_signal_does_not_crash():
    out = fade(np.ones(10, dtype=np.float32), ms=40)
    assert out.size == 10


def test_peak_normalize():
    out = peak_normalize(tone(0.2, 0.2), 0.85)
    assert float(np.max(np.abs(out))) == pytest.approx(0.85, abs=1e-4)


def test_peak_normalize_silence_is_noop():
    audio = silence(0.1)
    assert float(np.max(np.abs(peak_normalize(audio)))) == 0.0


def test_write_and_read_wav(tmp_path):
    data = tone(0.1, 0.4)
    p = write_wav16(tmp_path / "a.wav", data)
    assert p.exists()
    # 读回时按 16k 存一份才能被 read_wav_16k 接受
    import subprocess

    from videodub.media.ffmpeg import find_ffmpeg

    dst = tmp_path / "b.wav"
    subprocess.run([find_ffmpeg(), "-v", "error", "-i", str(p), "-ar", "16000",
                    "-ac", "1", str(dst)], check=True)
    back = read_wav_16k(dst)
    assert back.size > 0
    assert float(np.max(np.abs(back))) <= 1.0


def test_read_wav_rejects_wrong_rate(tmp_path):
    p = write_wav16(tmp_path / "a.wav", tone(0.05))       # 24kHz
    with pytest.raises(ValueError, match="16000|16k"):
        read_wav_16k(p)
