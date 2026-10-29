"""单 worker 串行队列。

**为什么必须串行**（不是偷懒）：

1. **显存**。Qwen2.5-3B fp16 约 6GB，Whisper large-v3 fp16 约 3GB。12GB 卡理论
   上能共存，但 CUDA 的碎片化分配器 + ctranslate2 自己的 arena 极易 OOM，
   而 Windows WDDM 下 OOM 的表现是「慢到卡死」而不是干净报错——很难排查。
2. **磁盘 IO**。配音阶段要同时读上百个 mp3、写上百 MB 的中间 wav，
   并发会互相拖死。
3. **体验**。串行并不阻止用户一次丢 5 个进来，界面上显示「排队 #2」就够了，
   用户反而觉得「它在按顺序干活」。

**为什么要开子进程而不是在本进程里加载模型**：

现有的「一阶段一进程」是最值钱的架构资产——子进程退出时 CUDA context 随进程
销毁，显存自动回收。改成进程内调用必须显式 ``del model; gc.collect();
torch.cuda.empty_cache()``，而 ctranslate2 与 torch 抢显存的事故极难复现。
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable

from ..errors import Cancelled, VideoDubError
from ..log import log
from ..progress import STAGES


@dataclass
class Task:
    job_id: str
    run: Callable[["Task"], None]
    cancel: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False


class SerialWorker:
    """一个线程 + 一个队列。提交即返回，执行严格串行。"""

    def __init__(self, max_queue: int = 10):
        self._q: queue.Queue[Task | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: Task | None = None
        self._pending: dict[str, Task] = {}
        self.max_queue = max_queue

    # -------------------------------------------------- 生命周期
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="videodub-worker",
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    # -------------------------------------------------- 提交 / 取消
    def submit(self, job_id: str, fn: Callable[[Task], None]) -> int:
        """返回排队位置（1 表示下一个就跑）。"""
        with self._lock:
            if len(self._pending) >= self.max_queue:
                raise RuntimeError(f"队列已满（{self.max_queue}）")
            task = Task(job_id=job_id, run=fn)
            self._pending[job_id] = task
            self._q.put(task)
            position = len(self._pending) + (1 if self._current else 0)
        self.start()
        return position

    def ensure_capacity(self, extra: int) -> None:
        """给「一次提交整个合集」临时放宽上限。

        默认上限（10）是防误触用的；用户明确点「处理整个合集」时，
        第 11 集被拒反而是 bug。上限只放宽、不回收——串行队列里
        多排几十个空任务的开销可以忽略。
        """
        with self._lock:
            # 就地算，**不能**调 self.depth：那是 property，会再抢一次同一把
            # 非重入锁，直接把请求线程锁死（一次提交整个合集就是永久挂起）。
            depth = len(self._pending) + (1 if self._current else 0)
            self.max_queue = max(self.max_queue, depth + max(0, extra))

    def cancel(self, job_id: str) -> bool:
        """取消。已经在跑的任务会在阶段边界/下载循环里退出。"""
        with self._lock:
            if self._current and self._current.job_id == job_id:
                self._current.cancelled = True
                self._current.cancel.set()
                log(f"已请求取消 {job_id}（当前阶段结束后停止）", "warn")
                return True
            if task := self._pending.pop(job_id, None):
                task.cancelled = True
                task.cancel.set()
                return True
        return False

    # -------------------------------------------------- 查询
    @property
    def current_id(self) -> str:
        cur = self._current
        return cur.job_id if cur else ""

    def position(self, job_id: str) -> int:
        with self._lock:
            if self._current and self._current.job_id == job_id:
                return 0
            ids = list(self._pending.keys())
            if job_id in ids:
                return ids.index(job_id) + 1
        return -1

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._pending) + (1 if self._current else 0)

    def is_busy(self) -> bool:
        return self.depth > 0

    # -------------------------------------------------- 循环
    def _loop(self) -> None:
        while True:
            task = self._q.get()
            if task is None:
                break
            if task.cancelled:
                with self._lock:
                    self._pending.pop(task.job_id, None)
                continue
            with self._lock:
                self._current = task
                self._pending.pop(task.job_id, None)
            try:
                if task.cancel.is_set():
                    raise Cancelled()
                task.run(task)
            except Cancelled:
                log(f"任务 {task.job_id} 已取消", "warn")
            except VideoDubError as exc:
                log(f"任务 {task.job_id} 失败：{exc.message}", "error")
            except Exception as exc:  # noqa: BLE001
                log(f"任务 {task.job_id} 异常：{exc}", "error")
                traceback.print_exc()
            finally:
                with self._lock:
                    self._current = None
                self._q.task_done()


STAGE_ORDER = STAGES
