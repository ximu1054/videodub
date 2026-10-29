"""测试公共设施。

两个原则：

  - **不碰真实用户数据**。所有写盘测试都把 ``VIDEODUB_HOME`` 指向临时目录，
    否则跑一次测试就会在用户的 %LOCALAPPDATA% 里塞一堆垃圾任务。
  - **不联网、不加载模型**。需要 GPU / 模型 / 下载器的用例统一打
    ``@pytest.mark.gpu`` 或 ``@pytest.mark.network``；``pyproject.toml`` 里
    默认 ``-m "not gpu and not network"``，所以本地一条 ``pytest`` 就能全绿。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# 收集阶段就会 import videodub.web.api，那时模块级 ``state = AppState()``
# 已经把真实用户目录绑进去了。所以必须在这里——**导入期**——就把家目录指走，
# 而不是等到 fixture 执行。否则跑一次测试会在用户的 %LOCALAPPDATA% 里留垃圾。
if not os.environ.get("VIDEODUB_HOME"):
    _sandbox = Path(tempfile.mkdtemp(prefix="videodub-test-"))
    os.environ["VIDEODUB_HOME"] = str(_sandbox)
    os.environ["VIDEODUB_CONFIG"] = str(_sandbox / "config.toml")


@pytest.fixture(scope="session")
def karpathy() -> dict:
    """30 分钟真实播客的时间轴样本（182 句，语音占 85.4%）。

    来历见 ``tests/fixtures/_build_karpathy.py``：全部时长读自旧项目的
    mp3 缓存，并已裁掉首尾静音。用 v0.1 算法跑这份样本得 16.9 秒漂移，
    与当年真实成片的 16.04 秒对得上——所以它是可信的回归基线。
    """
    payload = json.loads((FIXTURES / "karpathy_vibe_coding.json").read_text("utf-8"))
    return payload


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """把配置与任务目录都关进 tmp_path。"""
    root = tmp_path / "videodub-home"
    root.mkdir()
    monkeypatch.setenv("VIDEODUB_HOME", str(root))
    monkeypatch.setenv("VIDEODUB_CONFIG", str(root / "config.toml"))
    return root


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """给需要 ffmpeg 的用例提供一个「能跑但只返回固定字节」的假货。

    真正要解码音频的用例不该用它——那些用例标 ``gpu``/``net`` 之外的
    ``media`` 标记，靠 imageio-ffmpeg 自带的二进制跑。
    """
    exe = tmp_path / ("ffmpeg.bat" if sys.platform == "win32" else "ffmpeg")
    if sys.platform == "win32":
        exe.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
    else:
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("VIDEODUB_FFMPEG", str(exe))
    return exe
