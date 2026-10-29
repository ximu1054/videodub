"""进度事件与上报。

设计要点：**阶段实现（stages/*）不认识 Web**。它们只往一个 ``Reporter``
里 emit 结构化事件，CLI 把它打到终端，Web 把它转成 SSE 推给浏览器。
这样同一套阶段代码在两种入口下完全复用。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

STAGES: tuple[str, ...] = ("download", "asr", "translate", "render")
STAGE_LABELS: dict[str, str] = {
    "download": "下载",
    "asr": "识别",
    "translate": "翻译",
    "render": "配音",
}

# 各阶段在总进度里的权重，来自实测耗时比（30 分钟视频：下载12s/识别124s/翻译300s/配音181s）
_STAGE_WEIGHT: dict[str, float] = {
    "download": 0.02,
    "asr": 0.22,
    "translate": 0.48,
    "render": 0.28,
}


def compute_overall(stage: str, stage_percent: float, has_download: bool = True) -> float:
    """把「当前阶段 + 阶段内百分比」换算成总进度。"""
    weights = dict(_STAGE_WEIGHT)
    if not has_download:
        weights["download"] = 0.0
    total = sum(weights.values()) or 1.0
    done = 0.0
    for name in STAGES:
        w = weights[name] / total
        if name == stage:
            done += w * min(max(stage_percent, 0.0), 100.0) / 100.0
            break
        done += w
    return round(done * 100.0, 1)


@dataclass(frozen=True)
class ProgressEvent:
    """一条进度事件。字段名与前端约定一致，改动要同步 app.js。"""

    job_id: str
    seq: int
    kind: str = "progress"          # progress | stage | log | done | error
    stage: str = ""
    stage_index: int = 0
    stage_total: int = len(STAGES)
    stage_percent: float = 0.0
    overall_percent: float = 0.0
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    level: str = "info"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "seq": self.seq,
            "kind": self.kind,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "stage_total": self.stage_total,
            "stage_percent": round(self.stage_percent, 1),
            "overall_percent": self.overall_percent,
            "message": self.message,
            "metrics": self.metrics,
            "level": self.level,
            "ts": self.ts,
        }


@runtime_checkable
class Reporter(Protocol):
    """阶段实现只依赖这个协议。"""

    def stage_start(self, stage: str, message: str = "") -> None: ...
    def progress(self, stage: str, percent: float, message: str = "",
                 metrics: dict[str, Any] | None = None) -> None: ...
    def stage_done(self, stage: str, elapsed: float = 0.0, message: str = "") -> None: ...
    def log(self, text: str, level: str = "info") -> None: ...


def new_job_id() -> str:
    """20261029-103700-3f2a 形式，可读、可排序、不撞。"""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


class BaseReporter:
    """维护 seq 与阶段状态，子类只需实现 ``_send``。"""

    def __init__(self, job_id: str = "", has_download: bool = True,
                 sink: Callable[[dict[str, Any]], None] | None = None):
        self.job_id = job_id or new_job_id()
        self.has_download = has_download
        self._seq = 0
        self._sink = sink
        self._stage = ""

    # ---------------------------------------------------------- 内部
    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _make(self, **kw: Any) -> ProgressEvent:
        return ProgressEvent(job_id=self.job_id, seq=self._next(), **kw)

    def _send(self, event: ProgressEvent) -> None:  # pragma: no cover - 抽象
        if self._sink:
            self._sink(event.to_dict())

    def _emit(self, event: ProgressEvent) -> None:
        """子类覆盖以实现输出，默认只写 sink。"""
        self._send(event)

    # ---------------------------------------------------------- 协议实现
    def stage_start(self, stage: str, message: str = "") -> None:
        self._stage = stage
        idx = STAGES.index(stage) if stage in STAGES else 0
        self._emit(self._make(
            kind="stage", stage=stage, stage_index=idx + 1,
            stage_percent=0.0,
            overall_percent=compute_overall(stage, 0.0, self.has_download),
            message=message or f"{STAGE_LABELS.get(stage, stage)}开始",
            metrics={"state": "start"},
        ))

    def progress(self, stage: str, percent: float, message: str = "",
                 metrics: dict[str, Any] | None = None) -> None:
        self._stage = stage
        idx = STAGES.index(stage) if stage in STAGES else 0
        pct = min(max(float(percent), 0.0), 100.0)
        self._emit(self._make(
            kind="progress", stage=stage, stage_index=idx + 1, stage_percent=pct,
            overall_percent=compute_overall(stage, pct, self.has_download),
            message=message, metrics=metrics or {},
        ))

    def stage_done(self, stage: str, elapsed: float = 0.0, message: str = "") -> None:
        idx = STAGES.index(stage) if stage in STAGES else 0
        self._emit(self._make(
            kind="stage", stage=stage, stage_index=idx + 1, stage_percent=100.0,
            overall_percent=compute_overall(stage, 100.0, self.has_download),
            message=message or f"{STAGE_LABELS.get(stage, stage)}完成",
            metrics={"state": "done", "elapsed": round(elapsed, 1)},
        ))

    def log(self, text: str, level: str = "info") -> None:
        self._emit(self._make(kind="log", stage=self._stage, message=text, level=level))

    def done(self, result: dict[str, Any]) -> None:
        self._emit(self._make(kind="done", stage="render", stage_percent=100.0,
                              overall_percent=100.0, message="完成", metrics=result))

    def error(self, code: str, message: str, hint: str = "", stage: str = "",
              detail: str = "") -> None:
        self._emit(self._make(kind="error", stage=stage or self._stage,
                              stage_percent=0.0, overall_percent=0.0,
                              level="error", message=message,
                              metrics={"code": code, "hint": hint, "detail": detail}))

    def cancelled(self) -> None:
        self._emit(self._make(kind="error", stage=self._stage, level="warn",
                              message="已取消", metrics={"code": "CANCELLED"}))


class NullReporter(BaseReporter):
    def _emit(self, event: ProgressEvent) -> None:
        pass


class StdoutReporter(BaseReporter):
    """CLI 用：把事件压成一行行的终端输出。"""

    def __init__(self, job_id: str = "", has_download: bool = True, verbose: bool = False):
        super().__init__(job_id=job_id, has_download=has_download)
        self.verbose = verbose
        self._last_pct: dict[str, int] = {}

    def _emit(self, event: ProgressEvent) -> None:
        self._send(event)
        if event.kind == "log":
            mark = {"warn": "! ", "error": "✗ "}.get(event.level, "  ")
            print(f"      {mark}{event.message}", flush=True)
            return
        if event.kind == "stage":
            state = event.metrics.get("state")
            if state == "start":
                print(f"\n[{event.stage_index}/{event.stage_total}] "
                      f"{STAGE_LABELS.get(event.stage, event.stage)}", flush=True)
            elif state == "done":
                sec = event.metrics.get("elapsed", 0)
                suffix = f"，耗时 {sec:.0f} 秒" if sec else ""
                print(f"      完成{suffix}", flush=True)
            return
        if event.kind == "progress":
            pct = int(event.stage_percent)
            # 只在百分比变化时刷新，避免刷屏
            if self._last_pct.get(event.stage) == pct:
                return
            self._last_pct[event.stage] = pct
            bar_len = 24
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            msg = f" {event.message}" if event.message else ""
            print(f"\r      {bar} {pct:3d}%{msg}", end="\n" if pct >= 100 else "",
                  flush=True)
            return
        if event.kind == "error":
            print(f"\n  ✗ {event.message}", flush=True)
            if hint := event.metrics.get("hint"):
                print(f"    提示：{hint}", flush=True)
            if self.verbose and (detail := event.metrics.get("detail")):
                print(f"    详情：{detail}", flush=True)
