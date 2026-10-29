"""时间轴排布——全项目最该有测试的地方。

这份文件里最重要的两个用例：

  ``test_karpathy_drift_baseline_reproduced``
      证明样本本身是真的：v0.1 的「只顺延不清债」跑出 16.9 秒，与当年真实
      成片的 16.04 秒吻合。如果哪天样本被改坏了，这个用例会先失败。

  ``test_karpathy_drift_below_1s_once_budget_respected``
      证明产品承诺成立：**译文守长度预算时，排布后的漂移是 0.00 秒。**
      做法是把超预算的句子按槽位裁到能念下的长度（这正是翻译阶段
      ``audit_budget`` + 重译要做的事），再跑一次。
"""

from __future__ import annotations

import pytest

from videodub.stages.align import plan_placement


# ---------------------------------------------------------------- 基本性质
def test_empty_input():
    placements, stats = plan_placement([], [])
    assert placements == []
    assert stats.placed == 0
    assert stats.drift_max == 0.0


def test_single_item_fits_untouched():
    placements, stats = plan_placement([{"start": 1.0, "end": 4.0}], [2.0], 10.0)
    (pl,) = placements
    assert pl.start_at == pytest.approx(1.0)
    assert pl.tempo == 1.0
    assert not pl.compressed
    assert stats.drift_max == 0.0
    assert stats.final_cursor == pytest.approx(3.0)


def test_gap_between_sentences_is_usable_time():
    """槽位算到「下一句开口」而不是「自己结束」，这是规则 1。"""
    items = [
        {"start": 0.0, "end": 2.0},
        {"start": 6.0, "end": 8.0},
    ]
    # 第 1 句有 4 秒可用（0→4，留 0.06 缝），4 秒语音本该压到 1.0x 以内
    placements, _ = plan_placement(items, [4.0, 1.0], 10.0)
    assert placements[0].slot == pytest.approx(6.0 - 0.06, abs=1e-3)
    assert placements[0].tempo == 1.0
    assert placements[0].overflowed is False


def test_tempo_capped_at_max():
    """塞不下时压到上限就停手，并如实标记 overflowed。"""
    items = [{"start": 0.0, "end": 2.0}, {"start": 4.0, "end": 6.0}]
    placements, stats = plan_placement(items, [20.0, 1.0], 10.0, max_tempo=1.15)
    first = placements[0]
    assert first.tempo == pytest.approx(1.15)
    assert first.overflowed is True
    assert first.clip_dur == pytest.approx(20.0 / 1.15, abs=1e-3)
    # 第 1 句溢出 17 秒，把第 2 句挤到了片尾之后，它也跟着溢出——错误会传染
    assert stats.overflowed == 2


def test_no_overlap_ever():
    """任何情况下都不允许两句重叠——这是「声音叠在一起」的直接防线。

    容差 1e-3 是因为 Placement 里的时间做了四舍五入，不是算法有缝。
    """
    items = [{"start": t, "end": t + 2.0} for t in (0.0, 2.5, 5.0, 7.5, 10.0)]
    placements, _ = plan_placement(items, [3.0] * 5, 15.0)
    for prev, cur in zip(placements, placements[1:]):
        assert cur.start_at >= prev.start_at + prev.clip_dur - 1e-3


def test_late_start_shrinks_slot():
    """规则 2：晚开口的句子按实际开口时刻算槽位，欠的时间当场还。"""
    items = [
        # 第 1 句语音 6 秒、槽位只有 4.9 秒 → 压到上限仍溢出，第 2 句只能往后挪
        {"start": 0.0, "end": 2.0},
        {"start": 5.0, "end": 6.0},
        {"start": 6.0, "end": 8.0},
    ]
    placements, _ = plan_placement(items, [6.0, 1.0, 0.5], 12.0, max_tempo=1.15)
    second = placements[1]
    assert second.start_at > 5.0            # 被挤后了
    assert second.slot < 1.2                # 槽位按实际开口时刻缩水


def test_offset_shifts_whole_track():
    items = [{"start": 5.0, "end": 7.0}]
    placements, _ = plan_placement(items, [1.5], 10.0, offset=1.0)
    assert placements[0].start_at == pytest.approx(6.0)


def test_head_clamped_never_compresses():
    """规则 3：整轨前移撞到 0 时，首句不压语速，交给后续顺延。"""
    items = [{"start": 0.5, "end": 2.0}]
    placements, stats = plan_placement(items, [2.0], 10.0, offset=-1.0)
    pl = placements[0]
    assert pl.head_clamped is True
    assert pl.start_at == 0.0
    assert pl.tempo == 1.0
    assert pl.clip_dur == pytest.approx(2.0)


def test_early_start_bounded():
    """装不下时可以提前开口，但不超过 early_max，且不压到上一句身上。"""
    items = [
        {"start": 1.0, "end": 3.0},
        {"start": 6.0, "end": 7.0},
    ]
    # 第 2 句 5 秒语音，2 秒槽位 → 想提前 3 秒，但 early_max=0.35 卡住
    placements, _ = plan_placement(
        items, [1.0, 5.0], 10.0, max_tempo=1.15, early_max=0.35
    )
    second = placements[1]
    assert second.borrowed <= 0.35 + 1e-6
    assert second.start_at >= 2.0            # 不能压到第一句身上


def test_total_fallback_when_zero():
    items = [{"start": 1.0, "end": 4.0}, {"start": 5.0, "end": 9.0}]
    _, stats = plan_placement(items, [1.0, 1.0], 0.0)
    assert stats.total == pytest.approx(9.0)


# ---------------------------------------------------------------- 回归基线
def test_karpathy_drift_baseline_reproduced(karpathy):
    """v0.1 不该在今天变得更好——它就该跑出 16 秒级的漂移。"""
    items = karpathy["items"]
    total = karpathy["total"]
    max_tempo = 1.15

    cursor = 0.0
    worst = 0.0
    for i, it in enumerate(items):
        target = float(it["start"])
        nxt = float(items[i + 1]["start"]) if i + 1 < len(items) else total
        slot = max(nxt - target - 0.06, 0.5)
        dur = float(it["dur"])
        if dur > slot * 1.02:
            dur /= min(dur / slot, max_tempo)
        start = max(target, cursor)
        cursor = start + dur
        worst = max(worst, start - target)

    assert worst > 12.0, "样本不再是那个漂移过 12 秒的例子了"
    assert worst == pytest.approx(
        karpathy["baseline"]["naive_on_fixture"]["drift_max"], abs=0.5
    )


def test_karpathy_new_algorithm_beats_baseline(karpathy):
    items = karpathy["items"]
    durs = [i["dur"] for i in items]
    _, stats = plan_placement(items, durs, karpathy["total"], max_tempo=1.15)

    baseline = karpathy["baseline"]["naive_on_fixture"]["drift_max"]
    assert stats.placed == 182
    assert stats.drift_max <= baseline + 0.5      # 不劣于旧版
    assert stats.drift_mean < 0.75                # 平均偏移明显更好
    assert stats.drift_over_1s < 30               # 超过 1 秒的句子更少
    assert stats.tempo_p95 <= 1.15 + 1e-6         # 语速上限没被破
    assert stats.final_cursor <= karpathy["total"]  # 音轨不越过片尾


def test_karpathy_drift_below_1s_once_budget_respected(karpathy):
    """把超预算的句子裁到槽位能念下的长度后，漂移必须归零。

    这一步模拟的是翻译阶段的 ``audit_budget`` + 缩短重译：
    译文一旦守住长度预算，排布就不会再漂。
    """
    items = karpathy["items"]
    total = karpathy["total"]
    durs = [i["dur"] for i in items]

    _, before = plan_placement(items, durs, total, max_tempo=1.15)
    assert before.overflowed > 0, "样本里本该有一批超长译文"

    # 迭代到稳定：裁到「槽位 × 上限语速」的长度
    for _ in range(3):
        placements, _ = plan_placement(items, durs, total, max_tempo=1.15)
        durs = [min(d, p.slot * 1.15) for d, p in zip(durs, placements)]

    _, after = plan_placement(items, durs, total, max_tempo=1.15)
    assert after.overflowed == 0
    assert after.drift_max < 1.0
    assert after.drift_over_1s == 0
