"""``videodub doctor``：环境自检。

这个小命令的价值很具体——**它把「收集环境信息」变成一条可复制的命令**。
Issue 里只要有它的输出，一半的来回就省掉了。
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import app_home, config_path, data_dir, load_settings, model_cached
from .util import human_size


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    required: bool = True


def _py() -> Check:
    v = sys.version_info
    ok = (3, 11) <= (v.major, v.minor) < (3, 13)
    return Check(
        "Python", ok, f"{v.major}.{v.minor}.{v.micro}（{sys.executable}）",
        "" if ok else "需要 3.11 或 3.12（3.13 上部分依赖还没有预编译轮子）",
    )


def _ffmpeg() -> Check:
    from .media.ffmpeg import find_ffmpeg, find_ffprobe
    import subprocess

    try:
        exe = find_ffmpeg()
    except Exception as exc:  # noqa: BLE001
        return Check("ffmpeg", False, str(exc),
                     "pip install imageio-ffmpeg，或装系统 ffmpeg / 设 VIDEODUB_FFMPEG")
    version = ""
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20)
        version = (out.stdout or "").splitlines()[0] if out.stdout else ""
    except Exception:  # noqa: BLE001
        pass
    probe = find_ffprobe()
    detail = f"{exe}\n       {version}"
    if not probe:
        detail += "\n       ffprobe: 未找到（不影响使用，时长解析会走回退路径）"
    return Check("ffmpeg", True, detail)


def _torch() -> Check:
    try:
        import torch
    except ImportError:
        return Check(
            "PyTorch", False, "未安装",
            '本地翻译需要它：pip install -e ".[local-mt]"；只用 API 翻译可以不装',
            required=False,
        )
    cuda = getattr(torch.version, "cuda", None)
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        free, total = torch.cuda.mem_get_info(0)
        return Check(
            "PyTorch", True,
            f"{torch.__version__}（CUDA {cuda}）\n"
            f"       GPU: {torch.cuda.get_device_name(0)}\n"
            f"       显存: {total / 1024 ** 3:.1f} GB（空闲 {free / 1024 ** 3:.1f} GB）",
        )
    return Check(
        "PyTorch", True, f"{torch.__version__}（CPU 版，CUDA build={cuda}）",
        "没有可用 GPU：识别会慢很多。想用 GPU 请装 CUDA 版 torch，见 docs/install-gpu.md",
        required=False,
    )


def _whisper() -> Check:
    try:
        import ctranslate2
        import faster_whisper
    except ImportError as exc:
        return Check("faster-whisper", False, str(exc), "pip install faster-whisper")
    try:
        cuda_types = ctranslate2.get_supported_compute_types("cuda")
        gpu = f"CUDA 计算类型可用（{len(cuda_types)} 种）" if cuda_types else "无 CUDA 支持"
    except Exception:  # noqa: BLE001
        gpu = "无法探测 CUDA 支持"
    return Check("faster-whisper", True,
                 f"{faster_whisper.__version__} / ctranslate2 {ctranslate2.__version__}\n"
                 f"       {gpu}")


def _tts() -> Check:
    try:
        import edge_tts
    except ImportError as exc:
        return Check("edge-tts", False, str(exc), "pip install edge-tts")
    return Check(
        "edge-tts", True, getattr(edge_tts, "__version__", "已安装"),
        "语音合成需要联网（这是本工具唯一必须联网的环节）",
        required=False,
    )


def _ytdlp() -> Check:
    try:
        import yt_dlp
    except ImportError:
        return Check("yt-dlp", False, "未安装（只处理本地文件时可忽略）",
                     "pip install yt-dlp", required=False)
    return Check("yt-dlp", True, getattr(yt_dlp.version, "__version__", "已安装"))


def _youtube() -> Check:
    """真去要一次视频信息，确认「现在这台机器能不能下载」。

    光看 yt-dlp 装没装没用：装好了照样可能被 YouTube 因为节点 IP 拦下来，
    表现是排了一百集、每一集都下载失败。这一项把那个坑提前暴露出来，
    换节点之后跑一次 ``videodub doctor`` 就知道好没好。
    """
    from .stages.download import probe_access

    settings = load_settings()
    if settings.proxy == "none":
        note = "代理已关闭，直连会被拦"
    else:
        note = ""
    r = probe_access(settings)
    if r["ok"]:
        return Check("YouTube 下载", True, "能取到视频信息", required=False)
    detail = r["message"]
    if note:
        detail += f"\n       {note}"
    if r.get("cookies"):
        detail += f"\n       cookies: {r['cookies']}"
    return Check("YouTube 下载", False, detail, required=False)


def _models() -> Check:
    from .stages.asr import MODEL_REPOS

    settings = load_settings()
    repo = MODEL_REPOS.format(model=settings.asr_model)
    asr_ok = model_cached(repo)
    mt_ok = model_cached(settings.mt_local_model)
    parts = [
        f"识别 {settings.asr_model}: {'已缓存' if asr_ok else '需下载 2.9 GB'}",
        f"翻译 {settings.mt_local_model.split('/')[-1]}: "
        f"{'已缓存' if mt_ok else '需下载 5.8 GB（或改用 API 翻译）'}",
    ]
    return Check(
        "模型", asr_ok or settings.mt_backend == "api", "\n       ".join(parts),
        "" if asr_ok else "首次运行会自动下载：videodub models --download",
        required=False,
    )


def _disk() -> Check:
    target = data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target).free
    except OSError as exc:
        return Check("磁盘", False, str(exc), "检查数据目录权限")
    ok = free > 5 * 1024 ** 3
    return Check(
        "磁盘", ok, f"可用 {human_size(free)}（{target}）",
        "" if ok else "空间偏少：30 分钟视频处理峰值约需 1.5 GB",
        required=False,
    )


def _platform() -> Check:
    return Check(
        "系统", True,
        f"{platform.system()} {platform.release()} · "
        f"{platform.machine()} · Python {platform.python_version()}",
    )


def _paths() -> Check:
    return Check(
        "路径", True,
        f"配置: {config_path()}\n"
        f"       数据: {app_home()}\n"
        f"       版本: VideoDub {__version__}",
    )


def run_checks() -> list[Check]:
    return [
        _platform(), _py(), _paths(),
        _ffmpeg(), _whisper(), _torch(), _tts(), _ytdlp(), _youtube(),
        _models(), _disk(),
    ]


def format_report(results: list[Check]) -> str:
    lines = ["VideoDub 环境自检", "=" * 62]
    for r in results:
        mark = "✓" if r.ok else ("✗" if r.required else "○")
        lines.append(f"[{mark}] {r.name}: {r.detail}")
        if not r.ok and r.hint:
            lines.append(f"       → {r.hint}")
        elif r.ok and r.hint:
            lines.append(f"       · {r.hint}")
    bad = [r for r in results if not r.ok and r.required]
    lines.append("=" * 62)
    if bad:
        lines.append(f"有 {len(bad)} 项必需依赖缺失：{', '.join(r.name for r in bad)}")
    else:
        lines.append("必需项全部就绪。")
    lines.append("")
    lines.append("把以上内容整段贴进 issue，能省掉很多来回。")
    return "\n".join(lines)


__all__ = ["Check", "format_report", "run_checks", "Path"]
