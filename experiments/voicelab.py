"""男声音色试听：合成对比样音，供挑选。

用法：
  .venv/Scripts/python.exe scripts/voicelab.py
输出：
  out/试听_男声四选一.mp3
  out/试听_双男声搭配.mp3
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import edge_tts

sys.path.insert(0, str(Path(__file__).parent))
from common import ffmpeg, run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
TMP = ROOT / "tmp" / "voicelab"
RATE = "+10%"

VOICES = [
    ("zh-CN-YunxiNeural", "云希", "阳光 活泼，偏年轻"),
    ("zh-CN-YunjianNeural", "云健", "激情 解说感，偏有力"),
    ("zh-CN-YunyangNeural", "云扬", "专业 播报腔，偏稳重"),
    ("zh-CN-YunxiaNeural", "云夏", "童声，小孩音"),
]

# 单音色对比用同一段真实课程内容
SOLO_LINES = [
    "欢迎来到规范驱动开发课，本课程与 JetBrains 合作推出。",
    "今天咱们聊点实在的：怎么让编程助手真正听懂你的需求，而不是瞎猜。",
]


async def synth(text: str, voice: str, dst: Path) -> None:
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice, rate=RATE).save(str(dst))
            if dst.exists() and dst.stat().st_size > 1000:
                return
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            print(f"  重试 {attempt + 1}: {e}")
            await asyncio.sleep(1.5)


def to_wav(mp3: Path, wav: Path) -> None:
    run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(mp3),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=24000",
         "-ac", "1", "-ar", "24000", str(wav)])


def silence(ms: int, dst: Path) -> None:
    run([ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=24000:cl=mono", "-t", f"{ms / 1000:.3f}", str(dst)])


async def build_solo() -> Path:
    parts: list[Path] = []
    for i, (vid, cn, desc) in enumerate(VOICES):
        idx = i + 1
        print(f"[{idx}/4] {cn} ({vid})")
        head = f"第{['一', '二', '三', '四'][i]}个，{cn}。"
        for j, line in enumerate([head] + SOLO_LINES):
            mp3 = TMP / f"solo_{i}_{j}.mp3"
            wav = TMP / f"solo_{i}_{j}.wav"
            await synth(line, vid, mp3)
            to_wav(mp3, wav)
            parts.append(wav)
        gap = TMP / f"gap_{i}.wav"
        silence(700, gap)
        parts.append(gap)
    listfile = TMP / "solo_list.txt"
    listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    dst = OUT / "试听_男声四选一.mp3"
    run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c:a", "libmp3lame", "-b:a", "128k", str(dst)])
    return dst


async def build_duo() -> Path:
    """云希为主 + 另外两个轮流当配角，听搭配感。"""
    script = [
        ("zh-CN-YunxiNeural", "欢迎来到规范驱动开发课，本课程与 JetBrains 合作推出。"),
        ("zh-CN-YunjianNeural", "谢谢你，安德鲁。"),
        ("zh-CN-YunxiNeural", "等等，我都不知道你戴眼镜。"),
        ("zh-CN-YunjianNeural", "你说得对，其实我不需要戴。"),
        ("zh-CN-YunxiNeural", "好吧，我也这么想。接下来换成云扬配云希。"),
        ("zh-CN-YunyangNeural", "谢谢你，安德鲁。"),
        ("zh-CN-YunxiNeural", "等等，我都不知道你戴眼镜。"),
        ("zh-CN-YunyangNeural", "你说得对，其实我不需要戴。"),
        ("zh-CN-YunxiNeural", "无论如何，规范驱动开发有三个好处，你马上就能感受到。"),
    ]
    parts: list[Path] = []
    for j, (vid, line) in enumerate(script):
        mp3 = TMP / f"duo_{j}.mp3"
        wav = TMP / f"duo_{j}.wav"
        await synth(line, vid, mp3)
        to_wav(mp3, wav)
        parts.append(wav)
        gap = TMP / f"duogap_{j}.wav"
        silence(260, gap)
        parts.append(gap)
    listfile = TMP / "duo_list.txt"
    listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    dst = OUT / "试听_双男声搭配.mp3"
    run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c:a", "libmp3lame", "-b:a", "128k", str(dst)])
    return dst


def main() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print("== 四音色对比 ==")
    a = asyncio.run(build_solo())
    print(f"  -> {a}")
    print("== 双人搭配 ==")
    b = asyncio.run(build_duo())
    print(f"  -> {b}")


if __name__ == "__main__":
    main()
