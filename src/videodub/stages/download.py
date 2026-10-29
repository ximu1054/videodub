"""下载：把一个网址变成本地视频文件。

两个必须写死的参数：

1. **``--remote-components ejs:github``**
   新版 yt-dlp 把「JS 挑战求解」拆成了可下载组件，不显式启用就会直接报
   「找不到任何格式」。这个坑全网大量老教程都没提。
2. **格式串强制 ``vcodec^=avc1``**
   YouTube 的 4K 是 AV1 编码，很多 Windows 播放器播不了（黑屏或有画无声），
   而且体积大得多。对「换配音」这件事，1080p H.264 是正确取舍。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from ..config import Settings
from ..errors import DownloadError
from ..log import log
from ..progress import Reporter
from ..util import VIDEO_EXT, is_url
from .. import platforms

STAGE = "download"

_PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_TOTAL_RE = re.compile(r"of\s+~?\s*([\d.]+)\s*(\w+)")

COOKIE_NAMES = ("cookies.txt",)


def find_cookies(extra_dirs: list[Path] | None = None) -> Path | None:
    """按「最可能命中」的顺序找 cookies.txt。

    顺序：yt-dlp 配置目录 → 下载 → 桌面 → 用户指定目录。
    要求 >200 字节，避免把误建的空文件当成有效凭据。
    """
    candidates: list[Path] = []
    candidates.extend(platforms.current().cookies_dirs())
    for d in extra_dirs or []:
        if d and Path(d).is_dir():
            candidates.append(Path(d))

    for d in candidates:
        for name in COOKIE_NAMES:
            p = d / name
            if p.is_file() and p.stat().st_size > 200:
                return p
        if d.is_dir():
            for p in sorted(d.glob("*cookies*.txt")):
                if p.is_file() and p.stat().st_size > 200:
                    return p
    return None


def _base_cmd() -> list[str]:
    return [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",          # 防止用户的全局配置干扰我们的参数
        "--newline",                # 进度一行一行来，好解析
        "--no-warnings",
        "--remote-components", "ejs:github",
        "--no-playlist",            # 默认只下当前这一个，不炸整个播放列表
    ]


def _net_args(settings: Settings, cookies: Path | None) -> list[str]:
    args: list[str] = []
    if settings.proxy and settings.proxy != "none":
        proxy = platforms.current().detect_proxy() if settings.proxy == "auto" else settings.proxy
        if proxy:
            args += ["--proxy", proxy]
    elif settings.proxy != "none":
        proxy = platforms.current().detect_proxy()
        if proxy:
            args += ["--proxy", proxy]
    if cookies:
        args += ["--cookies", str(cookies)]
    return args


def get_title(url: str, settings: Settings, cookies: Path | None = None) -> str:
    cmd = [*_base_cmd(), "--print", "%(title)s", "--skip-download",
           *_net_args(settings, cookies if cookies is not None else find_cookies()), url]
    try:
        p = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
    return lines[-1] if lines else ""


def probe_url(url: str, settings: Settings) -> dict:
    """只取元数据，不下载。给界面的「粘贴链接后预览」用。"""
    cookies = find_cookies()
    cmd = [*_base_cmd(), "--dump-single-json", "--skip-download",
           *_net_args(settings, cookies), url]
    try:
        import json

        p = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "title": "", "duration": 0.0, "error": "yt-dlp 启动失败"}
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()
        return {"ok": False, "title": "", "duration": 0.0,
                "error": (tail[-1] if tail else "解析失败")[:300]}
    try:
        data = json.loads(p.stdout or "{}")
        return {
            "ok": True,
            "title": data.get("title") or "",
            "duration": float(data.get("duration") or 0.0),
            "uploader": data.get("uploader") or "",
            "thumbnail": data.get("thumbnail") or "",
            "error": "",
        }
    except (ValueError, TypeError):
        return {"ok": False, "title": "", "duration": 0.0, "error": "返回内容无法解析"}


def format_selector(max_height: int = 1080) -> str:
    h = int(max_height)
    return (
        f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"bv*[height<={h}][vcodec^=avc1]+ba/"
        f"bv*[height<={h}]+ba/b[height<={h}]/b"
    )


def download(
    url: str,
    workdir: Path,
    settings: Settings,
    reporter: Reporter | None = None,
) -> Path:
    """下载到 ``workdir/source.<ext>``，返回文件路径。"""
    workdir.mkdir(parents=True, exist_ok=True)
    cookies = find_cookies()
    if reporter:
        proxy = settings.proxy
        if proxy == "auto":
            proxy = platforms.current().detect_proxy() or "未检测到"
        reporter.log(f"代理：{proxy}")
        reporter.log(f"登录凭据：{cookies.name if cookies else '无（公开视频通常够用）'}")

    tmpl = str(workdir / "source.%(ext)s")
    cmd = [*_base_cmd(),
           "-f", format_selector(settings.max_height),
           "--merge-output-format", "mp4",
           "-o", tmpl,
           *_net_args(settings, cookies), url]

    proc = subprocess.Popen(  # noqa: S603
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        **platforms.current().popen_kwargs(),
    )
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        if m := _PCT_RE.search(line):
            pct = float(m.group(1))
            size = ""
            if tm := _TOTAL_RE.search(line):
                size = f" / {tm.group(1)} {tm.group(2)}"
            if reporter:
                reporter.progress(STAGE, min(pct, 99.5),
                                  message=f"下载中 {pct:.0f}%{size}",
                                  metrics={"percent": pct})
        elif line.startswith("[Merger]") and reporter:
            reporter.progress(STAGE, 99.8, message="合并音视频")

    stderr = ""
    if proc.stderr is not None:
        stderr = proc.stderr.read() or ""
    proc.wait()

    if proc.returncode != 0:
        detail = (stderr or "\n".join(tail)).strip()[-2500:]
        lower = detail.lower()
        hint = ""
        if "sign in to confirm" in lower or "not a bot" in lower:
            hint = ("YouTube 判定当前 IP 像机器人。用 Chrome 扩展 Get cookies.txt LOCALLY "
                    "登录后导出 cookies.txt 放进「下载」文件夹再试；或换一个住宅 IP 节点")
        elif "cookie" in lower:
            hint = "cookies 已失效，重新导出一次覆盖"
        elif "unsupported url" in lower:
            hint = "这个网址 yt-dlp 不支持"
        raise DownloadError(f"下载失败（yt-dlp 返回 {proc.returncode}）",
                            hint=hint, detail=detail)

    for f in workdir.iterdir():
        if f.stem == "source" and f.suffix.lower() in VIDEO_EXT:
            if reporter:
                size_mb = f.stat().st_size / 1024 / 1024
                reporter.progress(STAGE, 100, message=f"已下载 {size_mb:.0f} MB")
            return f
    raise DownloadError(
        "下载完成但找不到视频文件",
        hint="可能是分片命名异常，试着删掉任务后重跑",
        detail="\n".join(tail[-10:]),
    )


def is_remote(target: str) -> bool:
    return is_url(target)


def write_source_url(workdir: Path, url: str) -> None:
    (workdir / "source_url.txt").write_text(url, encoding="utf-8")


def read_source_url(workdir: Path) -> str:
    p = workdir / "source_url.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


__all__ = [
    "STAGE", "download", "find_cookies", "format_selector", "get_title",
    "is_remote", "log", "probe_url", "read_source_url", "write_source_url",
    "os",
]
