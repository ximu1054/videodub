"""下载失败的诊断：别只丢一句「yt-dlp 返回 1」。

用户真正需要知道的是两件互斥的事——「cookies 没登录」还是「节点 IP 被拦」，
因为修法完全相反（重新导出 / 换节点）。这几条测试锁住那个判断。
"""

from __future__ import annotations

import time

from videodub.config import Settings
from videodub.stages import download as dl

BOT_STDERR = (
    "ERROR: [youtube] xxxx: Sign in to confirm you're not a bot. "
    "This helps protect our community. Use --cookies-from-browser or --cookies "
    "for the authentication."
)


def _write_cookies(path, *, extra: str = "") -> None:
    """写一个够大的 Netscape 格式 cookies 文件（find_cookies 要求 >200 字节）。"""
    row = ".youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tabcdefghijk\n"
    path.write_text("# Netscape HTTP Cookie File\n" + row * 12 + extra, encoding="utf-8")


def _isolate(monkeypatch) -> None:
    """把「本机默认搜索目录」清空。

    不这么做，测试会去读真实机器上的 ``%APPDATA%\\yt-dlp\\cookies.txt``，
    结果取决于跑测试的那台机器装了什么——这种测试不能算数。
    """

    class _Fake:
        def cookies_dirs(self):
            return []

        def detect_proxy(self):
            return None

    monkeypatch.setattr(dl.platforms, "current", lambda: _Fake())


# ---------------------------------------------------------------- 挑哪一份
def test_find_cookies_prefers_newest(tmp_path, monkeypatch):
    """两份 cookies 都在时，用最新那份。

    Windows 上 %APPDATA%\\yt-dlp 排在「下载」前面，而扩展默认导出到「下载」。
    按顺序取第一个，用户重新导出的那份就永远轮不到——「换了 cookies 还是不行」。
    """
    _isolate(monkeypatch)
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old = old_dir / "cookies.txt"
    new = new_dir / "cookies.txt"
    _write_cookies(old)
    _write_cookies(new)

    past = time.time() - 7200
    import os

    os.utime(old, (past, past))

    assert dl.find_cookies([old_dir, new_dir]) == new


def test_find_cookies_ignores_tiny_file(tmp_path, monkeypatch):
    """几十字节的文件多半是误建的空壳，别当成凭据。"""
    _isolate(monkeypatch)
    tiny = tmp_path / "cookies.txt"
    tiny.write_text("x", encoding="utf-8")
    assert dl.find_cookies([tmp_path]) is None


# ---------------------------------------------------------------- 判断成因
def test_diagnose_bot_without_cookies():
    reason, hint = dl.diagnose(BOT_STDERR, None, Settings())
    assert reason == "bot-no-cookies"
    assert "cookies.txt" in hint


def test_diagnose_bot_cookies_not_logged_in(tmp_path, monkeypatch):
    """关键分支：文件在、但没登录态 → 该让用户重新导出，而不是去换节点。"""
    cookies = tmp_path / "cookies.txt"
    _write_cookies(cookies)
    monkeypatch.setattr(dl, "cookies_logged_in", lambda *a, **k: False)

    reason, hint = dl.diagnose(BOT_STDERR, cookies, Settings())
    assert reason == "cookies-not-logged-in"
    assert "没登录" in hint
    assert str(cookies) in hint          # 得说清读的是哪一份，否则用户找不到


def test_diagnose_bot_ip_blocked(tmp_path, monkeypatch):
    """另一种成因：登录态是好的，那就是节点 IP 的问题。"""
    cookies = tmp_path / "cookies.txt"
    _write_cookies(cookies)
    monkeypatch.setattr(dl, "cookies_logged_in", lambda *a, **k: True)
    monkeypatch.setattr(dl, "exit_ip", lambda *a, **k: "1.2.3.4")

    reason, hint = dl.diagnose(BOT_STDERR, cookies, Settings())
    assert reason == "ip-blocked"
    assert "1.2.3.4" in hint
    assert "节点" in hint


def test_diagnose_bot_unknown_login_state(tmp_path, monkeypatch):
    """网络不通问不出登录态时，别硬说成某一种。"""
    cookies = tmp_path / "cookies.txt"
    _write_cookies(cookies)
    monkeypatch.setattr(dl, "cookies_logged_in", lambda *a, **k: None)

    reason, _ = dl.diagnose(BOT_STDERR, cookies, Settings())
    assert reason == "bot"


def test_diagnose_network_error():
    reason, hint = dl.diagnose(
        "ERROR: Unable to download webpage: <urlopen error [Errno 111] "
        "Connection refused> (proxy)", None, Settings())
    assert reason == "network"
    assert "代理" in hint or "网络" in hint


def test_diagnose_gone_video():
    reason, _ = dl.diagnose("ERROR: [youtube] xx: Video unavailable", None, Settings())
    assert reason == "gone"


def test_diagnose_unknown_returns_empty():
    assert dl.diagnose("ERROR: something odd happened", None, Settings()) == ("", "")


# ---------------------------------------------------------------- cookie 头
def test_cookie_header_keeps_only_youtube(tmp_path):
    p = tmp_path / "cookies.txt"
    p.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tv1\n"
        ".bilibili.com\tTRUE\t/\tTRUE\t0\tbuvid3\tv2\n",
        encoding="utf-8",
    )
    header = dl._cookie_header(p)
    assert "SID=v1" in header
    assert "buvid3" not in header
