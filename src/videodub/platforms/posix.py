"""POSIX（macOS / Linux）实现。

本项目以 Windows 为一等公民，这里只保证核心链路可用；代理探测没有
Windows 那样可靠（系统代理设置格式五花八门），主要以环境变量为准。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from .base import PROXY_PORTS, BasePlatform, parse_proxy_value, port_open


class PosixPlatform(BasePlatform):
    name = "posix"

    # -------------------------------------------------- 代理
    def detect_proxy(self) -> str | None:
        for key_name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
            if found := parse_proxy_value(os.environ.get(key_name, "")):
                return found
        # 桌面环境下很多人用 Clash / v2ray 的默认端口
        for port in PROXY_PORTS[:4]:
            if port_open(port):
                return f"http://127.0.0.1:{port}"
        return None

    # -------------------------------------------------- 打开文件夹
    def open_in_explorer(self, path: Path) -> None:
        path = Path(path)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])  # noqa: S603,S607
            else:
                target = path.parent if path.is_file() else path
                subprocess.Popen(["xdg-open", str(target)])  # noqa: S603,S607
        except OSError:
            pass

    # -------------------------------------------------- 杀进程树
    def kill_tree(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return
        # 3 秒后还在就强杀
        import threading

        def _hard() -> None:
            import time

            time.sleep(3)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass

        threading.Thread(target=_hard, daemon=True).start()

    # -------------------------------------------------- 子进程组
    def popen_kwargs(self) -> dict:
        return {"start_new_session": True}
