"""把 Whisper 的碎片句合并成「翻译单元」，并校验译文长度是否装得下。

**这是长视频漂移的根因所在**，改动要点：

旧实现用「单元累计 320 个英文字符」当切分上限。320 字符的英文译成中文要念
60 秒左右，而时间槽只有 14 秒——单元粒度与时间预算完全脱钩。实测 30 分钟
视频最惨的一个单元：中文 38 字，槽位只剩 1.2 秒，最终全片漂移 16.04 秒。

现在改成**按时间预算切**（默认 8 秒目标 / 13 秒硬顶），并追加
``check_budget`` 在翻译后校验：装不下就让模型压缩译文重译，
而不是靠事后压语速硬塞。对照组（单元 5.7 秒、人工流程）漂移仅 0.04 秒。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SENT_END = re.compile(r"[.!?。！？]['\")\]]?$")
CJK = re.compile(r"[\u4e00-\u9fff]")

# 中文朗读速度（字/秒）。实测 182 个单元、平均 51 字、平均槽位 9.8 秒 → 5.2 字/秒，
# 保守取 4.6，给「留一点呼吸」留余地。
CHARS_PER_SEC = 4.6


@dataclass
class Unit:
    start: float
    end: float
    text: str
    ids: list[int] = field(default_factory=list)

    @property
    def dur(self) -> float:
        return max(self.end - self.start, 0.0)

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "text": self.text, "ids": self.ids}


@dataclass
class BudgetVerdict:
    """一条译文能不能装进它的时间槽。"""

    ok: bool
    estimated_sec: float
    slot_sec: float
    limit_chars: int
    ratio: float
    reason: str = ""

    def hint(self) -> str:
        """喂给翻译模型的长度约束。"""
        if self.ok:
            return ""
        return (f"译文太长：约 {self.estimated_sec:.1f} 秒的语音要装进 "
                f"{self.slot_sec:.1f} 秒，请压缩到 {self.limit_chars} 个汉字以内，保持原意。")


def build_units(
    sents: list[dict],
    *,
    target_sec: float = 8.0,
    hard_sec: float = 13.0,
    min_sec: float = 3.0,
    gap_split: float = 0.8,
    max_chars: int = 160,
) -> list[dict]:
    """合并成适合整段朗读的单元。

    收拢条件（按优先级，任一满足即切）：
      1. 时长到 ``hard_sec`` —— 硬顶，再长就装不下中文了
      2. 与下一句间隙超过 ``gap_split`` —— 讲者明显换气，自然边界
      3. 以句末标点结尾且已够 ``min_sec`` 长
      4. 时长到 ``target_sec``
      5. 累计字符超过 ``max_chars`` —— 兜底
    """
    units: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        text = " ".join(str(s.get("text", "")).strip() for s in cur).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            units.append({
                "start": round(float(cur[0]["start"]), 3),
                "end": round(float(cur[-1]["end"]), 3),
                "text": text,
                "ids": [s.get("id") for s in cur],
            })
        cur = []

    for i, s in enumerate(sents):
        if not str(s.get("text", "")).strip():
            continue
        cur.append(s)
        dur = float(cur[-1]["end"]) - float(cur[0]["start"])
        chars = len("".join(str(x.get("text", "")) for x in cur))
        ended = bool(SENT_END.search(str(s["text"]).strip()))
        nxt_gap = (
            float(sents[i + 1]["start"]) - float(s["end"])
            if i + 1 < len(sents) else 99.0
        )

        if dur >= hard_sec:
            flush()
        elif nxt_gap > gap_split:
            flush()
        elif ended and dur >= min_sec:
            flush()
        elif dur >= target_sec:
            flush()
        elif chars > max_chars:
            flush()
    flush()
    return units


# ---------------------------------------------------------------- 长度预算
def estimate_zh_seconds(text: str, chars_per_sec: float = CHARS_PER_SEC) -> float:
    """粗估一段中文要念多久。中英混排时按各自速率折算。"""
    if not text:
        return 0.0
    cjk = len(CJK.findall(text))
    rest = len(text) - cjk
    # 非中文部分（数字、英文术语）按每 3 个字符 ≈ 1 个音节估
    return cjk / chars_per_sec + rest / (chars_per_sec * 3.0)


def check_budget(
    unit: dict,
    zh: str,
    *,
    next_start: float | None = None,
    guard: float = 0.06,
    chars_per_sec: float = CHARS_PER_SEC,
    tolerance: float = 0.15,
    min_slot: float = 0.5,
) -> BudgetVerdict:
    """判断译文是否装得进时间槽。

    时间槽 = 到下一句开口之前（把句间停顿也算进可用时间），而不是自己的 end。
    """
    start = float(unit["start"])
    if next_start is None:
        slot = max(float(unit["end"]) - start, min_slot)
    else:
        slot = max(next_start - start - guard, min_slot)

    est = estimate_zh_seconds(zh, chars_per_sec)
    ratio = est / slot if slot > 0 else 99.0
    limit = max(int(slot * chars_per_sec * (1.0 - tolerance)), 4)
    ok = ratio <= 1.0 + tolerance
    reason = "" if ok else f"预计 {est:.1f}s / 槽位 {slot:.1f}s"
    return BudgetVerdict(
        ok=ok, estimated_sec=round(est, 2), slot_sec=round(slot, 2),
        limit_chars=limit, ratio=round(ratio, 3), reason=reason,
    )


def audit_budget(
    units: list[dict], translations: dict[str, str], **kw
) -> list[tuple[int, BudgetVerdict]]:
    """批量体检，返回所有超预算的单元。"""
    out: list[tuple[int, BudgetVerdict]] = []
    for i, u in enumerate(units):
        nxt = float(units[i + 1]["start"]) if i + 1 < len(units) else None
        v = check_budget(u, translations.get(str(i), ""), next_start=nxt, **kw)
        if not v.ok:
            out.append((i, v))
    return out


def units_to_dialogue(units: list[dict], translations: dict[str, str],
                      speaker: str = "A") -> list[dict]:
    """拼成 render 能吃的 dialogue 列表。

    缺译时退回原文——宁可念英文，也不要留一段空白（静音会被误认为卡住）。
    """
    items: list[dict] = []
    for i, u in enumerate(units):
        zh = (translations.get(str(i)) or "").strip() or u["text"]
        items.append({
            "id": str(i),
            "speaker": speaker,
            "start": round(float(u["start"]), 2),
            "end": round(float(u["end"]), 2),
            "zh": zh,
        })
    items.sort(key=lambda x: x["start"])
    return items
