"""翻译提示词。

从 ``translate.py`` 里提出来，因为原来它是一段写死的字符串，
既没法按目标语言切换，也没法在「译文超长」时追加不同的约束。
"""

from __future__ import annotations

BASE_RULES = (
    "你是专业的视频译者，负责把讲稿翻译成自然、口语化的简体中文，用于配音朗读。\n"
    "规则：\n"
    "1. 忠实原意，不增不减，不添加任何解释或旁白。\n"
    "2. 译文要像中国人说话，不要翻译腔，不要书面化长句。\n"
    "3. 专有名词、产品名、代码相关术语保留英文原文（如 Whisper、API、Python）。\n"
    "4. 严格按输入条数逐条输出，格式为「序号. 译文」，每行一条，不要合并或拆条。\n"
    "5. 只输出译文行，不要任何前言、总结或空行。\n"
    "6. 这是给「配音」用的，不是字幕：避免书面虚词，句子短、能一口气念完。"
)

SYSTEM_PROMPT = BASE_RULES

# 中文通常比英文长，紧凑句必须压语速才装得下。让模型先写短，比事后压语速好听得多。
LENGTH_RULE = (
    "\n7. 本批有字数限制：每条译文不得超过 {limit} 个汉字（含标点）。"
    "宁可精简表达，也不要超长——超长会导致配音语速变快。"
)


def build_system_prompt(limit_chars: int | None = None) -> str:
    if limit_chars and limit_chars > 0:
        return BASE_RULES + LENGTH_RULE.format(limit=limit_chars)
    return BASE_RULES


def build_user_prompt(items: list[str], context: str = "") -> str:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
    if context:
        return f"（视频主题：{context}）\n\n{numbered}"
    return numbered


def retranslate_prompt(text: str, limit_chars: int) -> str:
    """单条重译：明确给字数上限。"""
    return (
        f"请把下面这句话译成中文，必须控制在 {limit_chars} 个汉字以内（含标点），"
        f"保留原意，不要解释，只输出译文：\n{text}"
    )
