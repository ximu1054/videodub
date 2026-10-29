"""输入判定与工程量估算。看起来琐碎，但都是用户会踩的坑。"""

from __future__ import annotations

import pytest

from videodub.util import (estimate_disk, estimate_seconds, fmt_ts, hr, human_size,
                           is_url, normalize_input)


@pytest.mark.parametrize("text,expect", [
    ("https://www.youtube.com/watch?v=x", True),
    ("HTTP://example.com/a.mp4", True),
    ("www.youtube.com/watch?v=x", False),
    ("C:\\videos\\a.mp4", False),
    ("", False),
])
def test_is_url(text, expect):
    assert is_url(text) is expect


@pytest.mark.parametrize("raw,expect", [
    ('"/path/with space/a.mp4"', "/path/with space/a.mp4"),
    ("  '/tmp/a.mp4'  ", "/tmp/a.mp4"),
    ("  plain  ", "plain"),
    (None, ""),
])
def test_normalize_input(raw, expect):
    assert normalize_input(raw) == expect


def test_fmt_ts():
    assert fmt_ts(0) == "00:00:00.00"
    assert fmt_ts(83.456) == "00:01:23.46"
    assert fmt_ts(3723.5) == "01:02:03.50"
    assert fmt_ts(-5) == "00:00:00.00"


def test_hr():
    assert hr(83) == "1:23"
    assert hr(3723) == "1:02:03"
    assert hr(-1) == "0:00"


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(3 * 1024 ** 3) == "3.0 GB"


def test_estimate_seconds_has_floor():
    # 太短的视频也要给个非零估计，否则界面上显示「0 秒」
    assert estimate_seconds(1.0) == 20.0
    assert estimate_seconds(600.0) < estimate_seconds(600.0, gpu=False)


def test_estimate_disk_prefers_source_size():
    small = estimate_disk(60.0, source_bytes=100 * 1024 * 1024)
    big = estimate_disk(60.0, source_bytes=1000 * 1024 * 1024)
    assert big > small
    # 留了 200MB 余量给中间文件
    assert estimate_disk(0.0) >= 200 * 1024 * 1024


def test_gpu_info_never_raises():
    """没有 GPU、没有 torch、没有 nvidia-smi 的机器上也必须返回空值而不是崩。"""
    from videodub.util import gpu_info

    name, gb = gpu_info()
    assert isinstance(name, str)
    assert isinstance(gb, float)
    assert gb >= 0
