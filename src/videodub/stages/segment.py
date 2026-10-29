"""把词级时间戳合并成句级片段。

这是纯函数，不依赖任何模型——单测直接喂假 word 对象即可。
"""

from __future__ import annotations

import re

SENT_END = re.compile(r"[.!?。！？]['\")\]]?$")


def to_sentences(
    words,
    max_dur: float = 10.0,
    max_chars: int = 95,
    gap_split: float = 0.55,
    min_frag_sec: float = 0.9,
) -> list[dict]:
    """Whisper 按停顿切词，这里再按标点和长度合成句子。

    参数来自实测：``gap_split=0.55`` 能把「换气」和「句中停顿」分开；
    ``max_dur=10`` 保证单句不会长到没法作为一个时间单位。
    """
    segs: list[dict] = []
    cur: list = []

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        text = "".join(getattr(w, "word", "") for w in cur).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            segs.append({
                "start": float(cur[0].start),
                "end": float(cur[-1].end),
                "text": text,
            })
        cur = []

    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            dur = w.end - cur[0].start
            chars = len("".join(getattr(x, "word", "") for x in cur))
            if gap > gap_split or dur > max_dur or chars > max_chars:
                flush()
        cur.append(w)
        token = (getattr(w, "word", "") or "").strip()
        if token and SENT_END.search(token):
            flush()
    flush()

    # 太短的碎片并进上一句：避免「嗯」「好」单独成段，配音听起来一顿一顿的
    merged: list[dict] = []
    for s in segs:
        if merged and (
            s["end"] - s["start"] < min_frag_sec or s["text"] in {"-", "...", "—"}
        ):
            merged[-1]["end"] = s["end"]
            merged[-1]["text"] = (merged[-1]["text"] + " " + s["text"]).strip()
        else:
            merged.append(dict(s))
    return merged


def attach_ids(sents: list[dict]) -> list[dict]:
    for i, s in enumerate(sents):
        s["id"] = i
    return sents
