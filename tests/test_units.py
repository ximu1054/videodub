"""翻译单元切分与长度预算。

漂移的另一半在这里：光靠排布救不了「译文比原声长一倍」，
必须在翻译阶段就把句子压到装得下。
"""

from __future__ import annotations

from videodub.stages.units import (audit_budget, build_units, check_budget,
                                   estimate_zh_seconds, units_to_dialogue)


def _sent(start, end, text, sid=0):
    return {"start": start, "end": end, "text": text, "id": sid}


def test_build_units_splits_on_hard_limit():
    # 一段连续 20 秒没有句号的讲话，必须在 13 秒硬顶附近切开。
    # 切口只能落在句边界上，所以允许超出一句的长度（2 秒）。
    sents = [_sent(i * 2.0, i * 2.0 + 2.0, "word ", i) for i in range(10)]
    units = build_units(sents, target_sec=8.0, hard_sec=13.0)
    assert len(units) >= 2
    for u in units:
        assert u["end"] - u["start"] <= 13.0 + 2.0


def test_build_units_splits_on_long_gap():
    sents = [
        _sent(0.0, 2.0, "hello there", 0),
        _sent(5.0, 7.0, "another part", 1),      # 间隙 3 秒 > gap_split
    ]
    units = build_units(sents, gap_split=0.8)
    assert len(units) == 2


def test_build_units_keeps_short_complete_sentence():
    sents = [_sent(0.0, 0.8, "Yes.", 0), _sent(0.9, 1.6, "Sure.", 1)]
    units = build_units(sents)
    assert len(units) >= 1
    # 太短的两句不该被拆成两个单元（每个都念不出东西）
    assert units[0]["text"].startswith("Yes.")


def test_build_units_ids_preserved():
    sents = [_sent(0.0, 3.0, "one,", 7), _sent(3.1, 6.0, "two,", 8)]
    units = build_units(sents, gap_split=99.0, target_sec=99.0, hard_sec=99.0)
    assert len(units) == 1
    assert units[0]["ids"] == [7, 8]


def test_build_units_skips_blank():
    sents = [_sent(0.0, 1.0, "   ", 0), _sent(1.0, 4.0, "real text.", 1)]
    units = build_units(sents)
    assert len(units) == 1
    assert units[0]["text"] == "real text."


# ---------------------------------------------------------------- 预算
def test_estimate_zh_seconds_basic():
    # 46 个汉字按时速 4.6 字/秒 = 10 秒
    text = "我" * 46
    assert estimate_zh_seconds(text) == 46 / 4.6


def test_estimate_zh_seconds_mixed_text():
    # 中英混排：英文按 3 字符 ≈ 1 个音节折算
    zh = estimate_zh_seconds("中文" * 10)
    mixed = estimate_zh_seconds("中文" * 10 + "abcdef")
    assert mixed > zh
    assert mixed < zh + 1.0


def test_check_budget_ok():
    unit = {"start": 0.0, "end": 8.0}
    v = check_budget(unit, "这是一句正常长度的译文。", next_start=9.0)
    assert v.ok
    assert v.hint() == ""


def test_check_budget_too_long():
    unit = {"start": 0.0, "end": 3.0}
    v = check_budget(unit, "我" * 80, next_start=4.0)
    assert not v.ok
    assert v.limit_chars > 0
    assert "压缩到" in v.hint()
    assert v.slot_sec == 4.0 - 0.06


def test_check_budget_slot_includes_gap():
    """槽位算到下一句开口，不是自己的 end——和 align 的规则一致。"""
    unit = {"start": 0.0, "end": 2.0}
    v1 = check_budget(unit, "我" * 20, next_start=3.0)
    v2 = check_budget(unit, "我" * 20, next_start=8.0)
    assert v2.slot_sec > v1.slot_sec
    assert v2.ratio < v1.ratio


def test_audit_budget_flags_offenders():
    units = [
        {"start": 0.0, "end": 3.0},
        {"start": 4.0, "end": 12.0},
    ]
    translations = {"0": "我" * 60, "1": "短句。"}
    bad = audit_budget(units, translations)
    assert [i for i, _ in bad] == [0]


def test_units_to_dialogue_falls_back_to_source_text():
    units = [{"start": 0.0, "end": 3.0, "text": "hello"}]
    items = units_to_dialogue(units, {})
    assert items[0]["zh"] == "hello"       # 宁可念英文，也不留静音


def test_units_to_dialogue_sorts_by_start():
    units = [
        {"start": 5.0, "end": 7.0, "text": "b"},
        {"start": 0.0, "end": 2.0, "text": "a"},
    ]
    items = units_to_dialogue(units, {"0": "乙", "1": "甲"})
    assert [i["start"] for i in items] == [0.0, 5.0]
