"""平台无关的接口与共享实现。"""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

# Clash / v2ray / Surge 等常见本地代理端口，按命中概率排序
PROXY_PORTS: tuple[int, ...] = (7897, 7890, 7891, 7899, 9090, 10808, 10809, 1080, 2080)

# 沙箱 / 编辑器宿主会注入自己的代理端口，对普通用户没用，反而会把下载带偏
SANDBOX_PORT_HINTS: tuple[str, ...] = ("1987",)

# Windows 保留设备名：拿它当文件名会在某些路径下失败
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(text: str, limit: int = 60) -> str:
    """把视频标题变成安全的目录名。

    处理：非法字符、Windows 保留名、结尾的点与空格（Windows 会静默去掉）、
    超长截断、空串兜底。
    """
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s.upper().split(".")[0] in _WIN_RESERVED:
        s = f"_{s}"
    s = s[:limit].strip(" .")
    return s or "video"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def is_sandbox_proxy(value: str) -> bool:
    return any(hint in value for hint in SANDBOX_PORT_HINTS)


def parse_proxy_value(raw: str) -> str | None:
    """把注册表 / 环境变量里的代理写法统一成 ``http://host:port``。"""
    if not raw or is_sandbox_proxy(raw):
        return None
    if "=" in raw:
        for part in raw.split(";"):
            if part.lower().startswith("http="):
                raw = part.split("=", 1)[1]
                break
        else:
            return None
    if "://" not in raw:
        raw = "http://" + raw
    host, _, port = raw.rpartition(":")
    if not port.isdigit():
        return None
    if not port_open(int(port), host.replace("http://", "").replace("https://", "")):
        return None
    return raw


class BasePlatform:
    """子类实现与系统耦合的四个动作。"""

    name = "base"

    # -------------------------------------------------- 代理
    def detect_proxy(self) -> str | None:
        raise NotImplementedError

    # -------------------------------------------------- 打开文件夹
    def open_in_explorer(self, path: Path) -> None:  # pragma: no cover
        raise NotImplementedError

    # -------------------------------------------------- 杀进程树
    def kill_tree(self, pid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    # -------------------------------------------------- CUDA DLL
    def enable_cuda_dlls(self) -> list[str]:
        return []

    def cookies_dirs(self) -> list[Path]:
        """在哪些目录里找 cookies.txt。"""
        home = Path.home()
        dirs = [home / "Downloads", home / "Desktop"]
        return [d for d in dirs if d.is_dir()]

    # -------------------------------------------------- 起子进程（统一进程组）
    def popen_kwargs(self) -> dict:
        """让子进程能被整组杀掉，避免 ffmpeg 的孙子进程变孤儿占着文件锁。"""
        return {}


def _popen(cmd: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, **kw)  # noqa: S603
