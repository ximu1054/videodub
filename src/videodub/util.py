"""通用小工具：时间格式、体积格式、输入判定、工程量估算。"""

from __future__ import annotations

import re

VIDEO_EXT: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts", ".mpg", ".mpeg"}
)
AUDIO_EXT: frozenset[str] = frozenset({".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg"})

# 端到端耗时经验系数：实测 30 分钟视频在 12GB 显存上约 10 分钟
SPEED_RATIO_GPU = 0.33
SPEED_RATIO_CPU = 1.35


def fmt_ts(sec: float) -> str:
    """00:01:23.45"""
    m, s = divmod(max(sec, 0.0), 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def hr(sec: float) -> str:
    """1:23 / 1:02:03 —— 给人看的短格式。"""
    sec = int(max(sec, 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", (text or "").strip(), re.I))


def normalize_input(text: str) -> str:
    """去掉用户粘贴时带进来的引号和空白（Windows「复制为路径」会加引号）。"""
    return (text or "").strip().strip('"').strip("'").strip()


def estimate_seconds(duration: float, gpu: bool = True) -> float:
    return max(duration * (SPEED_RATIO_GPU if gpu else SPEED_RATIO_CPU), 20.0)


def estimate_disk(duration: float, source_bytes: int = 0) -> int:
    """估算单任务峰值磁盘占用（字节）。

    实测：源 118MB 的视频峰值约 3 倍。没有源文件大小时按时长粗估。
    """
    if source_bytes > 0:
        return int(source_bytes * 3.0) + 200 * 1024 * 1024
    # 1080p H.264 约 4 Mbit/s
    return int(duration * 0.5 * 1024 * 1024 * 3) + 200 * 1024 * 1024


def gpu_info() -> tuple[str, float]:
    """探测显卡型号与显存，返回 ``(名称, GB)``。

    三级回退，因为「有没有 GPU」和「装没装 torch」是两件事——

      - 只装基础依赖的用户没有 torch，但识别照样跑在 GPU 上
        （faster-whisper 走 CTranslate2，不需要 torch）
      - 所以不能只用 torch 判断，否则界面上永远显示「无 GPU」
    """
    # 1) torch：信息最全，但只有装了 local-mt 才有
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            gb = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            return name, round(gb, 1)
    except Exception:  # noqa: BLE001
        pass

    # 2) nvidia-smi：驱动自带，几乎总是可用
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run(
                [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=6, check=False,
            ).stdout.strip().splitlines()
            if out:
                name, _, mem = out[0].partition(",")
                return name.strip(), round(float(mem.strip()) / 1024, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    # 3) CTranslate2：只知道有几块，不知道型号
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "CUDA 设备", 0.0
    except Exception:  # noqa: BLE001
        pass
    return "", 0.0
