"""可用的中文音色。

**只有 6 个普通话**（3 女 3 男）+ 2 个方言。旧文档写「10 种中文音色」是不准确的，
这也是为什么自动双人配音在 v1 不做——素材池太窄，同性别音色差异很小。

``sample`` 供界面试听用同一句文案，保证横向可比。
"""

from __future__ import annotations

VOICES: list[dict] = [
    {
        "id": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "gender": "女",
        "tags": ["温暖", "通用"], "recommend": True,
        "desc": "最通用的默认选择，语气自然",
    },
    {
        "id": "zh-CN-YunjianNeural", "name": "云健", "gender": "男",
        "tags": ["沉稳", "解说"], "recommend": False,
        "desc": "偏低沉，适合教程与纪录片",
    },
    {
        "id": "zh-CN-YunxiNeural", "name": "云希", "gender": "男",
        "tags": ["阳光", "年轻"], "recommend": False,
        "desc": "轻快的年轻男声",
    },
    {
        "id": "zh-CN-XiaoyiNeural", "name": "晓伊", "gender": "女",
        "tags": ["活泼"], "recommend": False,
        "desc": "比晓晓更有起伏，适合口语化内容",
    },
    {
        "id": "zh-CN-YunyangNeural", "name": "云扬", "gender": "男",
        "tags": ["新闻", "专业"], "recommend": False,
        "desc": "新闻播音腔，偏正式",
    },
    {
        "id": "zh-CN-YunxiaNeural", "name": "云夏", "gender": "男",
        "tags": ["童声"], "recommend": False,
        "desc": "少年音，只适合特定题材",
    },
    {
        "id": "zh-CN-liaoning-XiaobeiNeural", "name": "小北", "gender": "女",
        "tags": ["东北话"], "recommend": False, "experimental": True,
        "desc": "方言音色，娱乐向",
    },
    {
        "id": "zh-CN-shaanxi-XiaoniNeural", "name": "小妮", "gender": "女",
        "tags": ["陕西话"], "recommend": False, "experimental": True,
        "desc": "方言音色，娱乐向",
    },
]

# 试听统一用这一句，保证各音色可比
PREVIEW_TEXT = "这段视频讲的是怎么把英文内容变成能直接听着走的中文音频。"

VOICE_IDS = {v["id"] for v in VOICES}


def get(voice_id: str) -> dict | None:
    for v in VOICES:
        if v["id"] == voice_id:
            return v
    return None


def display_name(voice_id: str) -> str:
    v = get(voice_id)
    return f"{v['name']}（{v['gender']}）" if v else voice_id.split("-")[-1].replace("Neural", "")
