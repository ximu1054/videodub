"""事件总线：把 worker 线程里产生的进度事件送到浏览器。

两个通道同时走：

  1. **内存队列** —— 实时订阅，页面开着就能看到
  2. **``events.jsonl``** —— 追加写盘，页面刷新时按 ``Last-Event-ID`` 回放

有第二个通道是必须的：用户刷新一下页面，进度不能从 0 重来，也不能一直停在
「等第一条事件」。
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

MAX_BUFFER = 200


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = {}

    # -------------------------------------------------- 发布
    def publish(self, job_id: str, event: dict[str, Any], *, persist: bool = True) -> None:
        if persist:
            self._append(job_id, event)
        with self._lock:
            queues = list(self._subs.get(job_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _append(self, job_id: str, event: dict[str, Any]) -> None:
        try:
            from ..jobs.store import job_dir

            d = job_dir(job_id)
            d.mkdir(parents=True, exist_ok=True)
            with (d / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass   # 事件落盘失败不该让任务挂掉

    # -------------------------------------------------- 订阅
    def subscribe(self, job_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=MAX_BUFFER)
        with self._lock:
            self._subs.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: queue.Queue) -> None:
        with self._lock:
            lst = self._subs.get(job_id, [])
            if q in lst:
                lst.remove(q)
            if not lst:
                self._subs.pop(job_id, None)

    # -------------------------------------------------- 回放
    def replay(self, job_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        try:
            from ..jobs.store import job_dir

            p = job_dir(job_id) / "events.jsonl"
        except Exception:  # noqa: BLE001
            return []
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(ev.get("seq", 0)) > after_seq:
                        out.append(ev)
        except OSError:
            return out
        return out[-500:]     # 别把整个历史都塞给浏览器

    def clear(self, job_id: str) -> None:
        try:
            from ..jobs.store import job_dir

            (job_dir(job_id) / "events.jsonl").unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._subs.pop(job_id, None)


bus = EventBus()
Path  # 保持 import 使用（类型提示在别处需要）
