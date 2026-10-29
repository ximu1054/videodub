"""平台适配层。

**注意包名是 ``platforms`` 带 s**。叫 ``platform`` 会遮蔽标准库的同名模块，
而 torch / transformers / numpy 在 Windows 上都会 ``import platform`` 取系统信息，
一旦遮蔽，报错信息离真正的原因有十万八千里。
"""

from __future__ import annotations

import sys

from .base import BasePlatform, safe_name

_impl: BasePlatform | None = None


def current() -> BasePlatform:
    global _impl
    if _impl is None:
        if sys.platform == "win32":
            from .windows import WindowsPlatform
            _impl = WindowsPlatform()
        else:
            from .posix import PosixPlatform
            _impl = PosixPlatform()
    return _impl


__all__ = ["BasePlatform", "safe_name", "current"]
