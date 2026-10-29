"""词级时间戳 → 句级片段。纯函数，喂假 word 对象即可。"""

from __future__ import annotations

from dataclasses import dataclass

from videodub.stages.segment import attach_ids, to_sentences


@dataclass
class W:
    word: str
    start: float
    end: float


def test_splits_on_sentence_punctuation():
    words = [
        W(" Hello", 0.0, 0.5), W(" there.", 0.5, 1.4),
        W(" How", 1.5, 1.8), W(" are", 1.8, 2.1), W(" you?", 2.1, 2.8),
    ]
    sents = to_sentences(words)
    assert [s["text"] for s in sents] == ["Hello there.", "How are you?"]


def test_splits_on_long_gap():
    words = [W(" one", 0.0, 0.5), W(" two", 2.0, 2.5)]   # 间隙 1.5 秒 > 0.55
    sents = to_sentences(words, min_frag_sec=0.0)
    assert len(sents) == 2


def test_splits_on_max_duration():
    words = [W(f" w{i}", i * 1.0, i * 1.0 + 0.9) for i in range(20)]
    sents = to_sentences(words, max_dur=5.0, min_frag_sec=0.0)
    assert len(sents) >= 3
    for s in sents:
        assert s["end"] - s["start"] <= 5.0 + 1e-6


def test_short_fragments_merge_back():
    """「嗯」「好」不该单独成段，否则配音一顿一顿的。"""
    words = [
        W(" This", 0.0, 0.5), W(" is", 0.5, 0.7), W(" fine.", 0.7, 1.2),
        W(" Right.", 1.3, 1.6),      # 只有 0.3 秒
    ]
    sents = to_sentences(words, min_frag_sec=0.9)
    assert len(sents) == 1
    assert "Right." in sents[0]["text"]


def test_placeholder_tokens_merge():
    words = [W(" ok.", 0.0, 1.5), W(" -", 1.5, 2.5)]
    sents = to_sentences(words)
    assert len(sents) == 1


def test_blank_words_dropped():
    words = [W("   ", 0.0, 0.5), W(" real", 0.5, 1.0), W(" text.", 1.0, 1.5)]
    sents = to_sentences(words)
    assert len(sents) == 1
    assert sents[0]["text"] == "real text."


def test_attach_ids_is_stable():
    sents = [{"start": 0.0}, {"start": 1.0}]
    assert [s["id"] for s in attach_ids(sents)] == [0, 1]
