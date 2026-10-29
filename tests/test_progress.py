"""进度事件：总进度换算、seq 单调、终态事件。

前端 ``app.js`` 直接吃这些字段名，改名等于破坏 API——所以这里把契约钉死。
"""

from __future__ import annotations

import pytest

from videodub.progress import (STAGES, BaseReporter, NullReporter, compute_overall,
                               new_job_id)


def test_compute_overall_monotonic():
    values = [compute_overall(s, 50) for s in STAGES]
    assert values == sorted(values)
    assert compute_overall(STAGES[-1], 100) == 100.0


def test_compute_overall_without_download():
    """本地文件没有下载阶段：识别从 0% 起步，而不是「已经走了 2%」。

    权重按剩余阶段重新归一化，所以跑完仍然是 100%。
    """
    assert compute_overall("asr", 0.0, has_download=True) == pytest.approx(2.0)
    assert compute_overall("asr", 0.0, has_download=False) == 0.0
    assert compute_overall("render", 100.0, has_download=False) == pytest.approx(100.0)


def test_compute_overall_clamps():
    assert compute_overall("asr", -50) == compute_overall("asr", 0)
    assert compute_overall("asr", 500) <= 100.0


def test_event_contract():
    """字段名 + 类型是与前端的契约。"""
    events: list[dict] = []
    r = BaseReporter(job_id="20261029-103700-3f2a", sink=events.append)
    r.stage_start("asr")
    r.progress("asr", 42.0, "识别中", {"done": 3})
    r.stage_done("asr", elapsed=12.5)
    r.done({"report": {}})

    assert [e["kind"] for e in events] == ["stage", "progress", "stage", "done"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[1]["overall_percent"] > 0
    assert events[1]["metrics"] == {"done": 3}
    assert events[0]["stage_index"] == 2 and events[0]["stage_total"] == 4
    assert events[3]["overall_percent"] == 100.0

    for key in ("job_id", "seq", "kind", "stage", "stage_index", "stage_total",
                "stage_percent", "overall_percent", "message", "metrics",
                "level", "ts"):
        assert key in events[0]


def test_error_event_carries_hint():
    events: list[dict] = []
    r = BaseReporter(sink=events.append)
    r.error("DOWNLOAD", "下载失败", hint="导出 cookies", stage="download")
    ev = events[-1]
    assert ev["kind"] == "error"
    assert ev["level"] == "error"
    assert ev["metrics"]["hint"] == "导出 cookies"
    assert ev["stage"] == "download"


def test_cancelled_event():
    events: list[dict] = []
    r = BaseReporter(sink=events.append)
    r.cancelled()
    assert events[-1]["metrics"]["code"] == "CANCELLED"
    assert events[-1]["level"] == "warn"


def test_null_reporter_is_silent():
    r = NullReporter()
    r.progress("asr", 50)      # 不抛异常、不输出
    r.done({})


def test_progress_clamped_to_0_100():
    events: list[dict] = []
    r = BaseReporter(sink=events.append)
    r.progress("asr", 150)
    assert events[-1]["stage_percent"] == 100.0
    r.progress("asr", -10)
    assert events[-1]["stage_percent"] == 0.0


def test_job_id_format():
    import re

    jid = new_job_id()
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{4}$", jid)


@pytest.mark.parametrize("stage", STAGES)
def test_every_stage_has_label(stage):
    from videodub.progress import STAGE_LABELS

    assert STAGE_LABELS[stage]
