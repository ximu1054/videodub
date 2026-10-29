"""ffmpeg 定位与调用。

三个关键设计：

1. **仓库不塞二进制**。优先级：``VIDEODUB_FFMPEG`` → 系统 PATH → ``imageio-ffmpeg``
   自带的构建（随 pip 装好，约 80MB，各平台都有轮子）。
2. **ffprobe 不是必需的**。项目里没有 ffprobe 也能跑——``duration_of`` 会退回读
   wav 头，再退回解析 ``ffmpeg -i`` 的 stderr。这顺便解决了旧版「ffmpeg 7.1 配
   ffprobe 9.0.2」的版本错配。
3. **失败时把 stderr 带出来**。旧版用 ``capture_output=True`` 然后只抛
   ``CalledProcessError``，用户完全看不到 ffmpeg 说了什么。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Iterator

from ..errors import MediaError, MissingDependencyError

_FFMPEG: str | None = None
_FFPROBE: str | None = None

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\S+.*?: Audio: (\w+).*?, (\d+) Hz, ([\w.()]+)")
_H264_FAMILY = {"h264", "vp8", "vp9", "av1", "theora"}


# ---------------------------------------------------------------- 定位
def find_ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG

    if env := os.environ.get("VIDEODUB_FFMPEG"):
        if Path(env).expanduser().exists():
            _FFMPEG = str(Path(env).expanduser())
            return _FFMPEG

    if found := shutil.which("ffmpeg"):
        _FFMPEG = found
        return _FFMPEG

    try:
        import imageio_ffmpeg

        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG
    except Exception as exc:  # noqa: BLE001
        raise MissingDependencyError(
            "找不到 ffmpeg",
            hint="安装 imageio-ffmpeg（pip install imageio-ffmpeg），"
                 "或装系统 ffmpeg，或用环境变量 VIDEODUB_FFMPEG 指定路径",
            detail=str(exc),
        ) from exc


def find_ffprobe() -> str | None:
    """ffprobe 是可选的，找不到返回 None。"""
    global _FFPROBE
    if _FFPROBE is not None:
        return _FFPROBE or None

    if env := os.environ.get("VIDEODUB_FFPROBE"):
        if Path(env).expanduser().exists():
            _FFPROBE = str(Path(env).expanduser())
            return _FFPROBE

    if found := shutil.which("ffprobe"):
        _FFPROBE = found
        return _FFPROBE

    # imageio-ffmpeg 的 ffmpeg 旁边偶尔带 ffprobe
    try:
        sibling = Path(find_ffmpeg()).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        if sibling.exists():
            _FFPROBE = str(sibling)
            return _FFPROBE
    except MissingDependencyError:
        pass

    _FFPROBE = ""
    return None


# ---------------------------------------------------------------- 调用
def run_ffmpeg(args: list[str], *, stage: str = "render",
               timeout: int | None = None) -> subprocess.CompletedProcess:
    """跑 ffmpeg。失败时抛 MediaError 并带上 stderr 尾部。"""
    cmd = [find_ffmpeg(), *args]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffmpeg 超时（{timeout} 秒）", hint="视频可能过长或磁盘过慢",
                         detail=" ".join(cmd[:8])) from exc
    except OSError as exc:
        raise MediaError("无法启动 ffmpeg", detail=str(exc)) from exc

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-3000:]
        raise MediaError(
            f"ffmpeg 返回 {proc.returncode}",
            hint=_ffmpeg_hint(tail),
            detail=tail or " ".join(cmd[:8]),
        )
    return proc


def _ffmpeg_hint(stderr: str) -> str:
    low = stderr.lower()
    if "no space left" in low:
        return "磁盘空间不足。清理旧任务，或用 videodub clean"
    if "invalid data found" in low or "moov atom not found" in low:
        return "视频文件损坏或没下载完整，重新获取源文件"
    if "permission denied" in low:
        return "文件被其他程序占用（播放器/资源管理器预览），关掉后重试"
    if "does not contain any stream" in low:
        return "这个文件里没有可用的音视频流"
    return "把这段 stderr 贴进 issue，通常能直接定位"


def stream_ffmpeg(args: list[str], *, stage: str = "render") -> Iterator[str]:
    """带进度地跑 ffmpeg：用 ``-progress pipe:1`` 读结构化进度行。"""
    cmd = [find_ffmpeg(), *args]
    proc = subprocess.Popen(  # noqa: S603
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        if proc.poll() is None:
            proc.kill()
        err = ""
        if proc.stderr is not None:
            err = proc.stderr.read() or ""
        proc.wait()
        if proc.returncode not in (0, None) and proc.returncode > 0:
            raise MediaError(f"ffmpeg 返回 {proc.returncode}",
                             hint=_ffmpeg_hint(err), detail=err.strip()[-3000:])


# ---------------------------------------------------------------- 探测
def duration_of(path: str | Path) -> float:
    """三级回退：ffprobe → wav 头 → 解析 ffmpeg -i。"""
    p = str(path)
    if probe_bin := find_ffprobe():
        try:
            out = subprocess.run(  # noqa: S603
                [probe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", p],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    try:
        with wave.open(p, "rb") as f:
            return f.getnframes() / float(f.getframerate())
    except (wave.Error, OSError, ZeroDivisionError):
        pass

    try:
        proc = subprocess.run(  # noqa: S603
            [find_ffmpeg(), "-i", p], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"无法读取时长：{Path(p).name}", detail=str(exc)) from exc

    if m := _DUR_RE.search(proc.stderr or ""):
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    raise MediaError(f"无法读取时长：{Path(p).name}", hint="文件可能不是有效的音视频")


def probe(path: str | Path) -> dict:
    """返回 duration / width / height / vcodec / acodec / channels / sample_rate。

    ``playable`` 表示浏览器能不能直接播（HEVC 不行，这会让用户以为文件坏了）。
    """
    p = str(path)
    info: dict = {"duration": 0.0, "width": 0, "height": 0, "vcodec": "",
                  "acodec": "", "channels": 0, "sample_rate": 0, "playable": True}

    if probe_bin := find_ffprobe():
        try:
            out = subprocess.run(  # noqa: S603
                [probe_bin, "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", p],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0:
                data = json.loads(out.stdout or "{}")
                info["duration"] = float(data.get("format", {}).get("duration") or 0.0)
                for st in data.get("streams", []):
                    if st.get("codec_type") == "video" and not info["vcodec"]:
                        info["vcodec"] = st.get("codec_name") or ""
                        info["width"] = int(st.get("width") or 0)
                        info["height"] = int(st.get("height") or 0)
                    elif st.get("codec_type") == "audio" and not info["acodec"]:
                        info["acodec"] = st.get("codec_name") or ""
                        info["channels"] = int(st.get("channels") or 0)
                        info["sample_rate"] = int(st.get("sample_rate") or 0)
                info["playable"] = info["vcodec"] in _H264_FAMILY or not info["vcodec"]
                return info
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    # 退化：解析 ffmpeg -i 的 stderr
    try:
        proc = subprocess.run(  # noqa: S603
            [find_ffmpeg(), "-i", p], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        err = proc.stderr or ""
    except (OSError, subprocess.SubprocessError):
        return info

    if m := _DUR_RE.search(err):
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    if m := _VIDEO_RE.search(err):
        info["vcodec"], info["width"], info["height"] = m.group(1), int(m.group(2)), int(m.group(3))
    if m := _AUDIO_RE.search(err):
        info["acodec"], info["sample_rate"] = m.group(1), int(m.group(2))
    info["playable"] = info["vcodec"] in _H264_FAMILY or not info["vcodec"]
    return info


# ---------------------------------------------------------------- 常用动作
def extract_audio_16k(video: str | Path, dst: str | Path) -> Path:
    """抽 16kHz 单声道 wav，喂给 Whisper。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-v", "error", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst),
    ], stage="asr")
    return dst


def to_m4a(src: str | Path, dst: str | Path, bitrate: str = "160k") -> Path:
    dst = Path(dst)
    run_ffmpeg(["-y", "-v", "error", "-i", str(src), "-vn",
                "-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart", str(dst)])
    return dst
