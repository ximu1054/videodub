"""FastAPI 应用装配与启动。

安全考虑（本地 Web 工具最廉价有效的防护）：**校验 Host 头**。
没有这一条，任意网页都能通过 DNS rebinding 调用你本机的接口、读走你的任务列表
和视频文件。只允许 127.0.0.1 / localhost / [::1]。
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .. import __version__
from ..config import Settings, app_home
from ..log import log
from .api import router

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, strict: bool = True):
        super().__init__(app)
        self.strict = strict

    async def dispatch(self, request: Request, call_next):
        if self.strict:
            host = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
            if host and host not in ALLOWED_HOSTS:
                return PlainTextResponse(
                    "VideoDub 只接受本机访问（Host 校验未通过）", status_code=403
                )
            origin = request.headers.get("origin")
            if origin and "://" in origin:
                oh = origin.split("://", 1)[1].split(":")[0].strip("[]").lower()
                if oh not in ALLOWED_HOSTS:
                    return PlainTextResponse("跨站请求被拒绝", status_code=403)
        return await call_next(request)


def create_app(strict_host: bool = True) -> FastAPI:
    app = FastAPI(
        title="VideoDub",
        version=__version__,
        description="把英文视频整条换成中文配音",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.add_middleware(LocalOnlyMiddleware, strict=strict_host)
    app.include_router(router, prefix="/api")

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        page = STATIC / "index.html"
        if not page.exists():
            return PlainTextResponse("前端资源缺失，请重新安装：pip install -e .")
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        icon = STATIC / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return PlainTextResponse("", status_code=204)

    @app.on_event("startup")
    def _startup() -> None:
        app_home().mkdir(parents=True, exist_ok=True)
        log(f"数据目录：{app_home()}")

    return app


def find_port(start: int, attempts: int = 20) -> int:
    for offset in range(attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{start}–{start + attempts} 之间没有可用端口")


def running_instance(port: int, *, timeout: float = 2.0) -> dict | None:
    """看看 ``127.0.0.1:port`` 上是不是已经有一个 VideoDub 在跑。

    有的话就把它的 ``/api/health`` 拿来用；不是我们（或没起来）就返回 None。

    为什么要问：``find_port`` 在端口被占时会顺延到 8766、8767 再起一个新的。
    用户双击两三次就会有三份进程，界面上看不出区别，改完代码后更是
    「明明更新了，看到的还是旧的样子」——因为打开的那个标签页连的是旧进程。
    """
    import json
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    # 端口上跑的可能是别的东西，认一下身份
    if not isinstance(data, dict) or "version" not in data:
        return None
    return data


def serve(settings: Settings, open_browser: bool = True) -> int:
    import uvicorn

    # 已经有一个在跑就别再起一个：多开的每一份都是独立的队列与日志，
    # 用户只会在两个界面之间反复刷新，最后分不清看的是哪一个。
    existing = running_instance(settings.port)
    if existing is not None:
        stale = existing.get("code_stale")
        if stale:
            print()
            print("  已经有一个 VideoDub 在运行，但它比代码旧，新功能不会出现。")
            print("  请先关掉任务栏里那个最小化的窗口，再双击本图标。")
            print()
        else:
            print()
            print(f"  VideoDub 已在运行：http://127.0.0.1:{settings.port}")
            print("  直接打开它就好（不用重复启动）。")
            print()
        if open_browser:
            try:
                webbrowser.open(f"http://127.0.0.1:{settings.port}")
            except Exception:  # noqa: BLE001
                pass
        return 0

    port = find_port(settings.port)
    strict = settings.host in ("127.0.0.1", "localhost", "")
    app = create_app(strict_host=strict)
    url = f"http://127.0.0.1:{port}"

    print()
    print("  " + "=" * 56)
    print("   VideoDub · 视频转中文配音")
    print("  " + "=" * 56)
    print(f"   界面地址   {url}")
    print(f"   数据目录   {app_home()}")
    if not strict:
        print(f"   ⚠ 监听 {settings.host}，已放宽 Host 校验，请自行确认网络边界")
    print("   按 Ctrl+C 停止")
    print()

    if open_browser:
        def _open() -> None:
            time.sleep(1.0)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_open, daemon=True).start()

    try:
        uvicorn.run(app, host="127.0.0.1" if strict else settings.host,
                    port=port, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        pass
    return 0


__all__ = ["create_app", "find_port", "running_instance", "serve", "STATIC"]
