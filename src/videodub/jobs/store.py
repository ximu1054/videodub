"""任务存储。

**不用 SQLite**，理由是：

  - 单用户本地场景，只有一个 worker 串行执行，没有并发写
  - 目录 + JSON 可读可调试，用户想手改参数、贴 job.json 报 bug 都方便
  - 断点续跑本来就靠「产物文件是否存在」，再引入数据库会产生两套真相
  - 100 个任务目录 ``glob + read_text`` 大约 5–10ms，够用

将来任务数上千或要做跨任务统计，只需换掉这个模块的实现，调用方不动。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import data_dir
from ..errors import UserInputError
from ..progress import STAGES, new_job_id

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")

# 每个阶段的「完成标志」产物。注意 source 是 glob（扩展名不定）
STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "download": ("source_url.txt",),
    "asr": ("transcript.json",),
    "translate": ("dialogue.json",),
    "render": ("dubbed.mp4",),
}

# 中间产物：删下游时要一起清掉
DERIVED: dict[str, tuple[str, ...]] = {
    "download": ("source.mp4", "source.mkv", "source.webm", "source.mov",
                 "source.m4v", "source.avi", "source.flv", "source.ts",
                 "source_url.txt"),
    "asr": ("transcript.json", "transcript.txt"),
    "translate": ("dialogue.json", "translated.json"),
    "render": ("dubbed.mp4", "dubbed_audio.m4a", "render_report.json"),
}

STATE_LABELS = {
    "queued": "排队中", "running": "运行中", "done": "完成",
    "failed": "失败", "canceled": "已取消", "draft": "草稿",
}


def jobs_root() -> Path:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_id(job_id: str) -> str:
    """防路径穿越：id 必须是「8位日期-6位时间-4位hex」。"""
    if not JOB_ID_RE.match(job_id or ""):
        raise UserInputError("任务 id 格式不正确")
    return job_id


def job_dir(job_id: str) -> Path:
    return jobs_root() / validate_id(job_id)


@dataclass
class Job:
    id: str
    created: str
    state: str = "draft"
    source: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    queue_position: int = 0

    # -------------------------------------------------- 构造
    @classmethod
    def create(cls, *, source: dict, params: dict, job_id: str | None = None) -> Job:
        workdir = job_dir(job_id or new_job_id())
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "tmp").mkdir(exist_ok=True)
        return cls(
            id=job_id or workdir.name,
            created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            state="queued",
            source=source,
            params=params,
            stages={name: {"state": "pending", "elapsed": 0.0} for name in STAGES},
        )

    # -------------------------------------------------- 路径
    @property
    def workdir(self) -> Path:
        return job_dir(self.id)

    @property
    def title(self) -> str:
        return self.source.get("title") or self.source.get("name") or self.id

    @property
    def duration(self) -> float:
        return float(self.source.get("duration") or 0.0)

    def file(self, name: str) -> Path:
        return self.workdir / name

    # -------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["title"] = self.title
        d["duration"] = self.duration
        d["state_label"] = STATE_LABELS.get(self.state, self.state)
        return d

    def save(self) -> None:
        p = self.workdir / "job.json"
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                     encoding="utf-8")

    @classmethod
    def load(cls, job_id: str) -> Job:
        p = job_dir(job_id) / "job.json"
        if not p.exists():
            raise UserInputError("任务不存在")
        raw = json.loads(p.read_text(encoding="utf-8"))
        known = {k: v for k, v in raw.items()
                 if k in cls.__dataclass_fields__}
        return cls(**known)

    # -------------------------------------------------- 阶段状态
    def stage_state(self, name: str) -> str:
        """``done`` / ``partial`` / ``pending``。

        ``partial`` 表示有中间产物但没完成标志——典型例子是翻译跑到一半
        （有 translated.json 没 dialogue.json），此时可以免费续跑。
        """
        flag = STAGE_ARTIFACTS.get(name, ())
        if flag and all(self.file(f).exists() for f in flag):
            if name == "download" and not self.source_video():
                return "partial"
            return "done"
        markers = {
            "asr": ("tmp/audio16k.wav",),
            "translate": ("translated.json",),
            "render": ("tmp/chinese_voice.wav",),
            "download": (),
        }[name]
        if any(self.file(m).exists() for m in markers):
            return "partial"
        return "pending"

    def source_video(self) -> Path | None:
        for ext in ("mp4", "mkv", "webm", "mov", "m4v", "avi", "flv", "ts"):
            p = self.workdir / f"source.{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def refresh_stages(self) -> None:
        for name in STAGES:
            if name == "download" and self.source.get("kind") != "url":
                self.stages.setdefault(name, {"state": "done", "elapsed": 0.0})
                self.stages[name]["state"] = "done"
                continue
            st = self.stage_state(name)
            self.stages.setdefault(name, {"state": "pending", "elapsed": 0.0})
            if self.stages[name]["state"] not in ("running", "failed"):
                self.stages[name]["state"] = st

    # -------------------------------------------------- 清理
    def clear_from(self, stage: str, *, keep_cache: bool = True) -> None:
        """删掉从某阶段起的产物，用于「重新运行这一步」。

        ``translated.json`` 在重跑 render 时保留——它是逐批断点，
        重跑翻译时也保留，因为改几句文案不值得重花 API 钱。
        """
        if stage not in STAGES:
            raise UserInputError(f"未知阶段：{stage}")
        idx = STAGES.index(stage)
        for name in STAGES[idx:]:
            for rel in DERIVED.get(name, ()):
                p = self.workdir / rel
                if p.exists() and p.is_file():
                    if keep_cache and name == "translate" and rel == "translated.json":
                        continue
                    if keep_cache and rel == "translated.json":
                        continue
                    p.unlink(missing_ok=True)
            self.stages[name] = {"state": "pending", "elapsed": 0.0}
        self.error = None
        self.result = {}
        if stage == "render" and not keep_cache:
            shutil.rmtree(self.workdir / "cache", ignore_errors=True)
        self.save()

    def clean_tmp(self) -> None:
        """成功之后删中间文件，磁盘峰值能从 3 倍源体积降到 1.x 倍。"""
        shutil.rmtree(self.workdir / "tmp", ignore_errors=True)

    def delete(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    # -------------------------------------------------- 展示
    def summary(self) -> dict[str, Any]:
        final = self.file("dubbed.mp4")
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "created": self.created,
            "duration": self.duration,
            "queue_position": self.queue_position,
            "stages": {
                name: {
                    "state": self.stages.get(name, {}).get("state", "pending"),
                    "elapsed": self.stages.get(name, {}).get("elapsed", 0.0),
                    "error": self.stages.get(name, {}).get("error", ""),
                }
                for name in STAGES
            },
            "has_video": final.exists(),
            "has_audio": self.file("dubbed_audio.m4a").exists(),
            "size_mb": round(final.stat().st_size / 1024 / 1024, 1) if final.exists() else 0,
            "report": self.result.get("report", {}),
            "error": self.error,
            "playable": self.source.get("playable", True),
        }


# ---------------------------------------------------------------- 集合操作
def list_jobs(limit: int = 50) -> list[Job]:
    out: list[Job] = []
    root = jobs_root()
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir() or not JOB_ID_RE.match(d.name):
            continue
        try:
            out.append(Job.load(d.name))
        except (OSError, ValueError, UserInputError, TypeError):
            continue
        if len(out) >= limit:
            break
    return out


def running_jobs() -> list[Job]:
    return [j for j in list_jobs(200) if j.state in ("running", "queued")]


def disk_usage() -> dict[str, float]:
    root = jobs_root()
    total = 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    return {"used_gb": round(total / 1024 ** 3, 2)}


def free_gb(path: Path | None = None) -> float:
    try:
        target = path or jobs_root()
        return round(shutil.disk_usage(target).free / 1024 ** 3, 1)
    except OSError:
        return 0.0


def cleanup(days: int = 7, *, keep_results: bool = True) -> tuple[int, int]:
    """删掉超过 N 天的任务。返回 ``(删除数, 释放字节)``。"""
    cutoff = time.time() - days * 86400
    removed = 0
    freed = 0
    for d in jobs_root().iterdir():
        if not d.is_dir() or not JOB_ID_RE.match(d.name):
            continue
        try:
            mtime = (d / "job.json").stat().st_mtime if (d / "job.json").exists() \
                else d.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue
        if keep_results and (d / "dubbed.mp4").exists():
            shutil.rmtree(d / "tmp", ignore_errors=True)
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
        freed += size
    return removed, freed
