"""翻译文本的解析与后处理。

模型输出的花样最多：全角点、顿号、括号、冒号、前后引号、编号从 0 开始……
这一层不测，线上就会出现「27 句译文只认回 24 句」。
"""

from __future__ import annotations

from videodub.stages.prompts import (build_system_prompt, build_user_prompt,
                                     retranslate_prompt)
from videodub.stages.translate import clean_line, is_chinese, parse_numbered


def test_clean_line_strips_bullets_and_quotes():
    assert clean_line("  - 你好  ") == "你好"
    assert clean_line('“你好”') == "你好"
    assert clean_line("* 你好") == "你好"


def test_parse_standard():
    raw = "1. 第一句\n2. 第二句\n3. 第三句"
    out = parse_numbered(raw, 3)
    assert out == {0: "第一句", 1: "第二句", 2: "第三句"}


def test_parse_tolerates_punctuation_variants():
    raw = "1、甲\n2．乙\n3：丙\n4) 丁\n5）戊"
    out = parse_numbered(raw, 5)
    assert out[0] == "甲" and out[1] == "乙"
    assert out[2] == "丙" and out[3] == "丁" and out[4] == "戊"


def test_parse_ignores_out_of_range():
    out = parse_numbered("1. ok\n99. 越界\n", 2)
    assert out == {0: "ok"}


def test_parse_ignores_chatter():
    raw = "好的，以下是翻译：\n1. 你好\n希望有帮助！"
    assert parse_numbered(raw, 1) == {0: "你好"}


def test_parse_empty():
    assert parse_numbered("", 3) == {}
    assert parse_numbered("没有任何编号", 3) == {}


def test_is_chinese():
    assert is_chinese("这是一句中文")
    assert not is_chinese("This is English")
    assert not is_chinese("")
    assert is_chinese("Opus 4.7 是目前最强的模型")


def test_system_prompt_mentions_length_only_when_limited():
    plain = build_system_prompt(None)
    limited = build_system_prompt(40)
    assert "配音" in plain
    assert "40" in limited


def test_user_prompt_numbers_from_one():
    prompt = build_user_prompt(["hello", "world"], context="")
    assert "1. hello" in prompt
    assert "2. world" in prompt


def test_retranslate_prompt_carries_limit():
    text = retranslate_prompt("一句很长的译文", 20)
    assert "20" in text
    assert "一句很长的译文" in text
