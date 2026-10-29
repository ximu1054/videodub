"""Windows 实现。

这里的每一条都是踩过坑才写出来的：
  - 代理要读注册表（Clash 写的不是环境变量）
  - 打开文件夹要用 explorer /select,（能高亮文件）
  - 杀进程必须 /T（否则 ffmpeg 的子进程变孤儿，文件锁不放）
  - CUDA DLL 要手动 add_dll_directory（pip 装的 nvidia 包不在 PATH 里）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .base import PROXY_PORTS, BasePlatform, parse_proxy_value, port_open


class WindowsPlatform(BasePlatform):
    name = "windows"

    # -------------------------------------------------- 代理
    def detect_proxy(self) -> str | None:
        """三级探测：注册表 → 常见端口 → 环境变量。

        环境变量放最后，因为某些宿主（编辑器沙箱）会注入自己的代理，
        那些端口对普通用户没用，反而会把下载带偏。
        """
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            winreg.CloseKey(key)
            if enable and server:
                if found := parse_proxy_value(str(server)):
                    return found
        except Exception:
            pass

        for port in PROXY_PORTS:
            if port_open(port):
                return f"http://127.0.0.1:{port}"

        for key_name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
            if found := parse_proxy_value(os.environ.get(key_name, "")):
                return found
        return None

    # -------------------------------------------------- 打开文件夹
    def open_in_explorer(self, path: Path) -> None:
        path = Path(path)
        try:
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603,S607
            else:
                subprocess.Popen(["explorer", str(path)])  # noqa: S603,S607
        except OSError:
            pass

    # -------------------------------------------------- 杀进程树
    def kill_tree(self, pid: int) -> None:
        try:
            subprocess.run(  # noqa: S603,S607
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # -------------------------------------------------- 子进程组
    def popen_kwargs(self) -> dict:
        # CREATE_NEW_PROCESS_GROUP 让 taskkill /T 能一次收掉整棵树
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}

    # -------------------------------------------------- CUDA DLL
    def enable_cuda_dlls(self) -> list[str]:
        """把 pip 安装的 cuBLAS / cuDNN DLL 目录挂上，供 ctranslate2 使用。

        没有这一步，faster-whisper 会报「找不到 cublas64_12.dll」，
        而这个报错完全看不出是 PATH 的问题。
        """
        import importlib.util

        if sys.platform != "win32":
            return []

        added: list[str] = []
        for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime"):
            try:
                spec = importlib.util.find_spec(pkg)
            except (ImportError, ValueError):
                continue
            if not spec or not spec.submodule_search_locations:
                continue
            base = Path(list(spec.submodule_search_locations)[0])
            for sub in ("bin", "lib"):
                d = base / sub
                if not d.is_dir():
                    continue
                try:
                    os.add_dll_directory(str(d))
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(d))
        return added

    # -------------------------------------------------- cookies
    def cookies_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        if appdata := os.environ.get("APPDATA"):
            dirs.append(Path(appdata) / "yt-dlp")
        dirs.extend(super().cookies_dirs())
        return dirs

    def web_browsers(self) -> list[str]:
        """给出可尝试的浏览器 cookie 来源（Chrome 127+ 因应用绑定加密通常失败）。"""
        found = []
        candidates = {
            "chrome": Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
            "edge": Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data",
            "firefox": Path(os.environ.get("APPDATA", "")) / "Mozilla/Firefox",
        }
        for name, path in candidates.items():
            if path.exists():
                found.append(name)
        return found
