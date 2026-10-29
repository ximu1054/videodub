"""音频处理原语：解码、裁静音、淡入淡出、写 wav。

这里保留了两处「便宜但效果显著」的手艺：
  - ``trim_silence``：裁掉 TTS 首尾空白，短句能省 0.3~0.8 秒，
    直接减轻后面必然要做的语速压缩。
  - ``fade``：每句首尾 40ms 淡入淡出，避免拼接处出现「咔」的爆音。
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

from .ffmpeg import find_ffmpeg

SR = 24000


def trim_silence(a: np.ndarray, keep_ms: int = 30) -> np.ndarray:
    """裁掉首尾静音。阈值取峰值 2% 与绝对下限 0.004 的较大者。"""
    if a.size == 0:
        return a
    env = np.abs(a)
    peak = float(env.max()) or 1.0
    thresh = max(0.004, peak * 0.02)
    idx = np.where(env > thresh)[0]
    if idx.size == 0:
        return a
    pad = int(SR * keep_ms / 1000)
    s = max(0, int(idx[0]) - pad)
    e = min(a.size, int(idx[-1]) + pad)
    return a[s:e]


def fade(a: np.ndarray, ms: int = 40) -> np.ndarray:
    n = min(int(SR * ms / 1000), a.size // 2)
    if n <= 0:
        return a
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    a[:n] *= ramp
    a[-n:] *= ramp[::-1]
    return a


def decode_f32(path: str | Path, tempo: float = 1.0) -> np.ndarray:
    """把 mp3/wav 解码成 24kHz 单声道 float32，可选变速（atempo）。"""
    cmd = [find_ffmpeg(), "-v", "error", "-i", str(path)]
    if abs(tempo - 1.0) > 0.005:
        cmd += ["-filter:a", f"atempo={tempo:.4f}"]
    cmd += ["-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    proc = subprocess.run(cmd, capture_output=True)  # noqa: S603
    if proc.returncode != 0:
        from ..errors import MediaError

        raise MediaError(
            "解码音频失败",
            detail=(proc.stderr or b"").decode("utf-8", "replace")[-2000:],
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def peak_normalize(canvas: np.ndarray, peak: float = 0.85) -> np.ndarray:
    current = float(np.max(np.abs(canvas))) if canvas.size else 0.0
    if current <= 0:
        return canvas
    canvas *= peak / current
    return canvas


def write_wav16(path: str | Path, data: np.ndarray, sr: int = SR) -> Path:
    """写 16bit 单声道 wav。用 stdlib wave，不引入 soundfile。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())
    return path


def read_wav_16k(path: str | Path) -> np.ndarray:
    """自己读 wav，绕开不同版本 PyAV 的接口差异（Whisper 可直接吃 ndarray）。"""
    with wave.open(str(path), "rb") as f:
        if f.getframerate() != 16000:
            raise ValueError(f"期望 16kHz，实际 {f.getframerate()}")
        if f.getnchannels() != 1:
            raise ValueError(f"期望单声道，实际 {f.getnchannels()}")
        raw = f.readframes(f.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
