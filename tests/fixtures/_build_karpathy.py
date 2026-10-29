"""一次性脚本：从旧项目的真实产物生成对齐回归样本。

不参与测试运行（文件名以下划线开头，pytest 不收集）。
保留它是因为样本的来历必须可追溯——否则下次有人看到 182 条
魔法数字只能选择相信。

数据来源：
  out_auto/<任务目录>/{dialogue.json, render_report.json, cache/*.mp3}
  dialogue.json  182 条，含原片 start/end 与中文译文
  cache/*.mp3    182 个，旧版 TTS 缓存，键为 sha1(f"{voice}|{rate}|{text}")[:16]

三个坑，都踩过了：

1. **时长必须裁过静音**。TTS 输出首尾各带 100~400ms 空白，render 阶段会先
   ``trim_silence`` 再排时间轴。直接拿 mp3 原长喂规划器，等于凭空多出
   六十多秒语音，样本就变成一个「不可能装下」的伪问题。
2. **19 条对不上缓存**。这些译文成片后被人工改过，缓存键自然变了；
   但它们对应的 mp3 一定还躺在 cache 里——182 个文件正好对上 182 句，
   163 句已点名，剩下 19 个文件就是这 19 句的。按「字数序 ↔ 时长序」
   一对一配对：中文朗读时长与字数单调正相关，这个配对在总量上是准的，
   逐句可能有个别错位，但总语音量精确——而漂移主要由总量决定。
3. **不能按字速估算**。上一版这么干，把 1541 个字估成 273 秒，实际只有
   162 秒（译文里混着英文和数字，字速远快于纯中文）。凭空多出的 110 秒
   让漂移假性飙到 33 秒，跟当年的 16 秒对不上。

产出：karpathy_vibe_coding.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SRC = Path(
    r"C:\Users\10549\WorkBuddy\2026-10-27-11-08-15\dub\out_auto"
    r"\Andrej Karpathy：Vibe Coding 只是入门，真正拉开差距的是 Agentic + Opus 5.5"
)
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+8%"
OUT = Path(__file__).with_name("karpathy_vibe_coding.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def cache_name(text: str) -> str:
    key = f"{VOICE}|{RATE}|{text}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".mp3"


def voiced_seconds(path: Path) -> float:
    """解码 → 裁首尾静音 → 得到 render 实际占用的时长。"""
    from videodub.media.audio import SR, decode_f32, trim_silence

    return len(trim_silence(decode_f32(path))) / SR


def naive_drift(items: list[dict], total: float, max_tempo: float = 1.15) -> float:
    """v0.1 的排布函数（只顺延、不清债），给样本标定「修之前有多糟」。"""
    cursor = 0.0
    worst = 0.0
    n = len(items)
    for i, it in enumerate(items):
        target = float(it["start"])
        nxt = float(items[i + 1]["start"]) if i + 1 < n else total
        slot = max(nxt - target - 0.06, 0.5)
        dur = float(it["dur"])
        if dur > slot * 1.02:
            dur /= min(dur / slot, max_tempo)
        start = max(target, cursor)
        cursor = start + dur
        worst = max(worst, start - target)
    return round(worst, 2)


def main() -> int:
    dialogue = json.loads((SRC / "dialogue.json").read_text(encoding="utf-8"))
    report = json.loads((SRC / "render_report.json").read_text(encoding="utf-8"))
    total = round(float(report["duration"]), 2)

    rows, unmatched, used = [], [], set()
    for row in dialogue:
        zh = row["zh"].strip()
        f = SRC / "cache" / cache_name(zh)
        if f.exists():
            used.add(f.name)
            dur = round(voiced_seconds(f), 3)
        else:
            dur = 0.0
            unmatched.append(len(rows))
        rows.append({
            "start": round(float(row["start"]), 3),
            "end": round(float(row["end"]), 3),
            "chars": len(zh),
            "dur": dur,
        })

    leftovers = sorted(
        (round(voiced_seconds(p), 3), p.name)
        for p in (SRC / "cache").glob("*.mp3") if p.name not in used
    )
    if len(leftovers) != len(unmatched):
        raise SystemExit(
            f"对不上：{len(unmatched)} 句缺时长，却剩 {len(leftovers)} 个缓存文件"
        )
    # 字数序 ↔ 时长序 一对一
    order = sorted(unmatched, key=lambda i: rows[i]["chars"])
    for i, (dur, _name) in zip(order, leftovers, strict=True):
        rows[i]["dur"] = dur

    payload = {
        "note": (
            "真实产物回归样本：30 分钟英文播客的中文配音时间轴，182 句。"
            "dur 是裁掉首尾静音后的有效语音长度（render 阶段实际占用的时间），"
            "全部 182 条都读自旧项目 mp3 缓存。"
        ),
        "source": "Andrej Karpathy: Vibe Coding 只是入门，真正拉开差距的是 Agentic",
        "voice": VOICE,
        "rate": RATE,
        "total": total,
        "baseline": {
            "real_run": {
                "note": "当年那次真实成片，数字记在 render_report.json 里",
                "drift_max": report["drift_max"],
                "tempo_compressed": report["tempo_compressed"],
                "tempo_avg": report["tempo_avg"],
            },
            "naive_on_fixture": {
                "note": "v0.1 算法跑这份样本得到的漂移，测试用它做对照",
                "drift_max": naive_drift(rows, total),
            },
        },
        "items": rows,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    span = sum(r["dur"] for r in rows)
    print(f"{len(rows)} 条全部实测，总时长 {total}s")
    print(f"语音总量 {span:.1f}s，占视频 {span / total * 100:.1f}%")
    print(f"v0.1 在这份样本上的漂移："
          f"{payload['baseline']['naive_on_fixture']['drift_max']}s"
          f"（当年真实成片 {report['drift_max']}s）")
    print(f"→ {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
