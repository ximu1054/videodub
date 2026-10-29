"""事件总线：内存队列 + 落盘回放。

刷新页面要能接着看进度，靠的就是 ``events.jsonl`` 回放 +
``Last-Event-ID``。这条链路断了，用户会以为任务卡住了。
"""

from __future__ import annotations

import queue

from videodub.web.bus import EventBus

JID = "20261029-103700-3f2a"


def drain(q: queue.Queue) -> list[dict]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_publish_and_subscribe():
    bus = EventBus()
    q = bus.subscribe(JID)
    bus.publish(JID, {"seq": 1, "kind": "progress"})
    assert drain(q)[0]["seq"] == 1
    bus.unsubscribe(JID, q)


def test_publish_without_subscriber_does_not_crash():
    EventBus().publish(JID, {"seq": 1})


def test_isolated_by_job():
    bus = EventBus()
    q = bus.subscribe(JID)
    bus.publish("20261029-110000-aaaa", {"seq": 1})
    assert drain(q) == []


def test_replay_after_seq():
    bus = EventBus()
    bus.clear(JID)
    for i in range(1, 6):
        bus.publish(JID, {"seq": i})
    assert [e["seq"] for e in bus.replay(JID, 3)] == [4, 5]


def test_replay_survives_restart():
    first = EventBus()
    first.clear(JID)
    first.publish(JID, {"seq": 1, "kind": "progress"})
    first.publish(JID, {"seq": 2, "kind": "done"})

    second = EventBus()          # 模拟进程重启后的新实例
    assert [e["seq"] for e in second.replay(JID, 0)] == [1, 2]


def test_replay_capped():
    bus = EventBus()
    bus.clear(JID)
    for i in range(1, 901):
        bus.publish(JID, {"seq": i})
    assert len(bus.replay(JID, 0)) == 500


def test_clear_removes_history():
    bus = EventBus()
    bus.publish(JID, {"seq": 1})
    bus.clear(JID)
    assert bus.replay(JID, 0) == []


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    q = bus.subscribe(JID)
    bus.unsubscribe(JID, q)
    bus.publish(JID, {"seq": 9})
    assert drain(q) == []


def test_replay_bad_job_id_returns_empty():
    assert EventBus().replay("../../etc", 0) == []


def test_replay_skips_corrupt_lines():
    bus = EventBus()
    bus.clear(JID)
    from videodub.jobs.store import job_dir

    with (job_dir(JID) / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json}\n")
        f.write('{"seq": 7}\n')
    assert [e["seq"] for e in bus.replay(JID, 0)] == [7]
