"""串行队列的容量控制。

这里只测一件容易出事的事：``ensure_capacity`` 在持锁状态下不能再调用
任何会抢同一把锁的成员。曾经写成 ``self.max_queue = max(..., self.depth ...)``，
而 ``depth`` 是带锁的 property、锁又是非重入的——结果是一次提交整个合集时
请求线程永久挂起，界面上表现为「点了没反应，一直在转圈」。
"""

from __future__ import annotations

import threading

from videodub.jobs.queue import SerialWorker


def _run_with_deadline(fn, seconds: float = 3.0) -> bool:
    """在子线程里跑 fn，返回它是否在期限内跑完。

    用线程而不是直接调用：真死锁时直接调用会让整个测试进程卡住，
    测试框架只能看到「没输出」，看不出是哪一条。
    """
    done = threading.Event()
    threading.Thread(target=lambda: (fn(), done.set()), daemon=True).start()
    return done.wait(seconds)


def test_ensure_capacity_does_not_deadlock():
    """持锁 + 再拿同一把锁 = 永久挂起。这条测试专门盯它。"""
    worker = SerialWorker(max_queue=10)

    finished = _run_with_deadline(lambda: worker.ensure_capacity(80))

    assert finished, "ensure_capacity 没有在 3 秒内返回——多半又出现了自锁"
    assert worker.max_queue >= 80


def test_ensure_capacity_only_grows():
    """上限只放宽、不回收：反复调用不应该把上限改小。"""
    worker = SerialWorker(max_queue=10)

    assert _run_with_deadline(lambda: worker.ensure_capacity(50))
    assert worker.max_queue == 50

    assert _run_with_deadline(lambda: worker.ensure_capacity(5))
    assert worker.max_queue == 50


def test_ensure_capacity_ignores_negative():
    worker = SerialWorker(max_queue=10)
    assert _run_with_deadline(lambda: worker.ensure_capacity(-99))
    assert worker.max_queue == 10
