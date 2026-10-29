"""日志：统一格式，全部走 stdout（Web 模式由 worker 捕获后转成 SSE 事件）。

设计取舍：不用 stdlib logging 的 handler 树，是因为本项目需要
「同一个 Logger 对象既能打到终端、又能被 worker 截获转成事件」。
用一个轻量的 Reporter 协议更直接（见 progress.py）。
"""

from __future__ import annotations

import os
import sys
import time

_QUIET = False


def set_quiet(quiet: bool = True) -> None:
    global _QUIET
    _QUIET = quiet


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def log(text: str, level: str = "info") -> None:
    """打印一行。level 只影响前缀，不改变流向。"""
    if _QUIET and level in ("debug", "info"):
        return
    prefix = {"debug": "  · ", "info": "  ", "warn": "  ! ", "error": "  ✗ "}.get(level, "  ")
    print(f"{prefix}{text}", flush=True)


def banner(text: str) -> None:
    if _QUIET:
        return
    line = "=" * 62
    print(f"\n{line}\n  {text}\n{line}", flush=True)


def debug(text: str) -> None:
    if os.environ.get("VIDEODUB_DEBUG"):
        print(f"[{_stamp()}] DBG {text}", file=sys.stderr, flush=True)
