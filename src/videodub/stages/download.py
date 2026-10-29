"""下载：把一个网址变成本地视频文件。

两个必须写死的参数：

1. **``--remote-components ejs:github``**
   新版 yt-dlp 把「JS 挑战求解」拆成了可下载组件，不显式启用就会直接报
   「找不到任何格式」。这个坑全网大量老教程都没提。
2. **格式串强制 ``vcodec^=avc1``**
   YouTube 的 4K 是 AV1 编码，很多 Windows 播放器播不了（黑屏或有画无声），
   而且体积大得多。对「换配音」这件事，1080p H.264 是正确取舍。

**合集（播放列表）怎么处理**：``probe_playlist`` 只读列表页（``--flat-playlist``），
一次拿到全部集数与标题；真正的下载仍然是一集一个任务，靠 ``--playlist-items``
（或用条目自己的 url）精确取第 N 集。不做「一个任务下完整个列表」——
那样进度、断点续跑、单集重试全都要重写。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

# 列表页里出现这些标题说明那一集已经不可用，占着集数但没有内容
_SKIP_TITLES = {"[Private video]", "[Deleted video]", "Private video", "Deleted video"}

# 一次最多列这么多集。再长的列表页交给用户自己填「只处理前 N 集」
PLAYLIST_ENTRY_CAP = 500


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


def _base_cmd(*, no_playlist: bool = True) -> list[str]:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",          # 防止用户的全局配置干扰我们的参数
        "--newline",                # 进度一行一行来，好解析
        "--no-warnings",
        "--remote-components", "ejs:github",
    ]
    if no_playlist:
        # 默认只下当前这一个，不炸整个播放列表
        cmd.append("--no-playlist")
    return cmd


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


def _query_param(url: str, key: str) -> str:
    try:
        return (parse_qs(urlparse(url).query).get(key) or [""])[0]
    except ValueError:
        return ""


def _entry_url(entry: dict) -> str:
    """列表页里占位条目的直链。

    YouTube 的 flat 条目直接带完整 url；别的站点偶尔只给 id，
    这时返回空串，调用方回退成「播放列表地址 + 第几集」的方式下载。
    """
    url = str(entry.get("url") or entry.get("webpage_url") or "")
    if url.startswith("http"):
        return url
    vid = str(entry.get("id") or "")
    if vid and "youtube" in str(entry.get("ie_key") or "").lower():
        return f"https://www.youtube.com/watch?v={vid}"
    return ""


def probe_playlist(url: str, settings: Settings) -> dict:
    """看这个链接是单个视频还是一个合集，是合集就把每集列出来。

    与 :func:`probe_url` 的关键差别是**不加** ``--no-playlist``：``watch?v=…&list=…``
    这种链接在 yt-dlp 里默认会被当成整个播放列表，正是我们要的信息。
    ``--flat-playlist`` 让它只读列表页、不逐个解析视频——实测 150 集约 3 秒，
    逐个解析会慢到几分钟。
    """
    cookies = find_cookies()
    cmd = [*_base_cmd(no_playlist=False), "--flat-playlist", "-J",
           "--skip-download", *_net_args(settings, cookies), url]
    try:
        p = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return _pl_fail("yt-dlp 启动失败")
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()
        return _pl_fail((tail[-1] if tail else "解析失败")[:300])
    try:
        data = json.loads(p.stdout or "{}")
    except (ValueError, TypeError):
        return _pl_fail("返回内容无法解析")
    if not isinstance(data, dict):
        return _pl_fail("返回内容无法解析")

    raw = data.get("entries")
    is_playlist = data.get("_type") == "playlist" or (
        isinstance(raw, list) and len(raw) > 1
    )

    entries: list[dict] = []
    if isinstance(raw, list):
        for i, e in enumerate(raw, 1):
            if not isinstance(e, dict):        # 解析失败的条目会是 None
                continue
            title = str(e.get("title") or "").strip()
            if not title or title in _SKIP_TITLES:
                continue
            entries.append({
                "index": i,                   # 保留在列表里的原始序号
                "id": str(e.get("id") or ""),
                "title": title,
                "duration": float(e.get("duration") or 0.0),
                "url": _entry_url(e),
            })
    capped = entries[:PLAYLIST_ENTRY_CAP]

    # 「单个视频」模式下要处理哪一集：看链接里的 v= 参数
    current = 1
    vid = _query_param(url, "v")
    if vid:
        for e in capped:
            if e["id"] == vid:
                current = e["index"]
                break

    return {
        "ok": True,
        "is_playlist": bool(is_playlist),
        "id": str(data.get("id") or ""),
        "title": str(data.get("title") or "").strip(),
        "count": len(capped),
        "total": len(entries),
        "entries": capped,
        "current_index": current,
        "duration": float(data.get("duration") or 0.0),
        "uploader": str(data.get("uploader") or data.get("channel") or ""),
        "thumbnail": str(data.get("thumbnail") or ""),
        "error": "",
    }


def _pl_fail(msg: str) -> dict:
    return {
        "ok": False, "is_playlist": False, "id": "", "title": "",
        "count": 0, "total": 0, "entries": [], "current_index": 0,
        "duration": 0.0, "uploader": "", "thumbnail": "", "error": msg,
    }


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
    *,
    item: int | None = None,
) -> Path:
    """下载到 ``workdir/source.<ext>``，返回文件路径。

    ``item`` 给定时表示「从播放列表里只取第 N 集」——此时把 ``--no-playlist``
    换成 ``--playlist-items N``。一集一个任务，进度条和断点续跑都还是原来的语义。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    cookies = find_cookies()
    if reporter:
        proxy = settings.proxy
        if proxy == "auto":
            proxy = platforms.current().detect_proxy() or "未检测到"
        reporter.log(f"代理：{proxy}")
        reporter.log(f"登录凭据：{cookies.name if cookies else '无（公开视频通常够用）'}")
        if item:
            reporter.log(f"合集第 {item} 集")

    tmpl = str(workdir / "source.%(ext)s")
    cmd = [*_base_cmd(no_playlist=item is None),
           "-f", format_selector(settings.max_height),
           "--merge-output-format", "mp4",
           "-o", tmpl]
    if item:
        cmd += ["--playlist-items", str(int(item))]
    cmd += [*_net_args(settings, cookies), url]

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
    "is_remote", "log", "probe_playlist", "probe_url", "read_source_url",
    "write_source_url", "os",
]
