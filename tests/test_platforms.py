"""文件名清洗与平台探测。

``safe_name`` 是安全边界：视频标题直接来自 YouTube，里面有 ``..``、
``/``、Windows 保留名（CON、NUL）都不奇怪。这条防线不能松。
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys

import pytest

from videodub.platforms import current, safe_name
from videodub.platforms.base import is_sandbox_proxy, parse_proxy_value, port_open


def test_platform_singleton():
    p = current()
    assert p is current()
    assert p.name in ("windows", "macos", "linux")


@pytest.mark.parametrize("raw,expect_absent", [
    ("Andrej Karpathy: Vibe Coding", ":"),
    ("a/b\\c", "/"),
    ('weird"name', '"'),
    ("pipe|name", "|"),
    ("star*name", "*"),
    ("question?name", "?"),
])
def test_safe_name_strips_illegal(raw, expect_absent):
    assert expect_absent not in safe_name(raw)


def test_safe_name_blocks_traversal():
    assert "/" not in safe_name("../../etc/passwd")
    assert "\\" not in safe_name("..\\..\\windows\\system32")
    assert safe_name("..") not in ("..", ".")


def test_safe_name_windows_reserved():
    if sys.platform != "win32":
        pytest.skip("保留名只对 Windows 有意义")
    for name in ("CON", "nul", "COM1", "LPT9"):
        cleaned = safe_name(name)
        assert cleaned.upper().rstrip(".") != name.upper()


def test_safe_name_truncates():
    out = safe_name("长" * 200, limit=60)
    assert len(out) <= 60


def test_safe_name_keeps_chinese_and_emoji_free():
    out = safe_name("中文标题：第一集")
    assert "中文标题" in out


def test_safe_name_never_empty():
    for raw in ("", "   ", "...", "///"):
        assert safe_name(raw)


def test_safe_name_leading_dots_removed():
    """以点开头在 Linux 上是隐藏文件，用户会以为任务丢了。"""
    assert not safe_name(".hidden").startswith(".")


# ---------------------------------------------------------------- 代理
@contextlib.contextmanager
def live_port():
    """起一个临时监听端口。

    backlog 开大一点：``parse_proxy_value`` 会真的 connect 一次做探测，
    backlog=1 时第二次探测会被系统直接拒掉，看起来像「端口没开」。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(16)
        yield s.getsockname()[1]


def test_parse_proxy_value_requires_live_port():
    """解析时会真的去连一下——代理设置了但没启动，等于没代理。"""
    with live_port() as port:
        assert parse_proxy_value(f"http://127.0.0.1:{port}") == f"http://127.0.0.1:{port}"


def test_parse_proxy_value_adds_scheme():
    with live_port() as port:
        assert parse_proxy_value(f"127.0.0.1:{port}") == f"http://127.0.0.1:{port}"


def test_parse_proxy_value_dead_port_returns_none():
    with live_port() as port:
        pass                     # 端口已经关了
    assert parse_proxy_value(f"http://127.0.0.1:{port}") is None


@pytest.mark.parametrize("raw", ["", "   ", "no-port-here"])
def test_parse_proxy_value_garbage(raw):
    assert parse_proxy_value(raw) is None


def test_parse_proxy_value_reads_windows_registry_format():
    """``ProxyServer`` 的写法是 ``http=host:port;https=host:port``。"""
    with live_port() as port:
        raw = f"https=127.0.0.1:1;http=127.0.0.1:{port}"
        assert parse_proxy_value(raw) == f"http://127.0.0.1:{port}"


def test_sandbox_proxy_detected():
    # 1987 是某些沙箱环境注入的端口，不该被当成用户代理
    assert is_sandbox_proxy("http://127.0.0.1:1987") is True
    assert is_sandbox_proxy("http://127.0.0.1:7890") is False


def test_port_open_on_closed_port():
    with live_port() as port:
        pass
    assert port_open(port) is False


def test_port_open_on_live_socket():
    with live_port() as port:
        assert port_open(port) is True


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 有 %LOCALAPPDATA%")
def test_windows_home_dirs_exist():
    p = current()
    for d in p.cookies_dirs():
        assert os.path.isabs(str(d))
