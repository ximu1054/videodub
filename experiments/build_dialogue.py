"""把分批写好的中文译文合并成 dialogue.json。

译文分片放在 <out>/parts/*.json，每个文件形如：

  {"0": "甲说的中文", "4": [["A", "甲的半句"], ["B", "乙的半句"]]}

key 是 units.json 里的单元序号；值是字符串（单人）或 [说话人, 文本] 列表（同一单元内换人）。
换人时按字数比例切分该单元的时间区间。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def expand(idx: int, start: float, end: float, val) -> list[dict]:
    if isinstance(val, str):
        return [{"speaker": "A", "start": start, "end": end, "zh": val.strip()}]
    segs = [(sp, txt.strip()) for sp, txt in val if txt.strip()]
    if not segs:
        return []
    if len(segs) == 1:
        return [{"speaker": segs[0][0], "start": start, "end": end, "zh": segs[0][1]}]
    # 按中文长度分配时长；每段留 0.04s 间隔
    total = sum(len(t) for _, t in segs) or 1
    span = end - start
    out, cur = [], start
    for k, (sp, txt) in enumerate(segs):
        share = span * len(txt) / total
        seg_end = end if k == len(segs) - 1 else round(cur + share - 0.04, 2)
        out.append({"speaker": sp, "start": round(cur, 2), "end": seg_end, "zh": txt})
        cur = round(cur + share, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = Path(args.out)
    units = json.load(open(root / "units.json", encoding="utf-8"))
    parts = sorted((root / "parts").glob("*.json"))
    if not parts:
        raise SystemExit("没有找到译文分片")

    merged: dict[str, object] = {}
    for p in parts:
        data = json.load(open(p, encoding="utf-8"))
        for k, v in data.items():
            if k in merged and merged[k] != v:
                print(f"  ! 分片冲突，后者覆盖：单元 {k}（{p.name}）")
            merged[k] = v

    items: list[dict] = []
    missing = []
    for i, u in enumerate(units):
        v = merged.get(str(i))
        if v is None:
            missing.append(i)
            continue
        items.extend(expand(i, u["start"], u["end"], v))

    items.sort(key=lambda x: float(x["start"]))
    for k, it in enumerate(items):
        it["id"] = str(k)
        it["start"] = round(float(it["start"]), 2)
        it["end"] = round(float(it["end"]), 2)

    dst = root / "dialogue.json"
    json.dump(items, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    a = sum(1 for x in items if x["speaker"] == "A")
    print(f"合并 {len(parts)} 个分片 → {len(items)} 条对话（A {a} / B {len(items)-a}）")
    print(f"已覆盖单元 {len(merged)}/{len(units)}")
    if missing:
        print(f"未覆盖 {len(missing)} 个：{missing[:20]}{' ...' if len(missing) > 20 else ''}")
    print(f"写入 {dst}")


if __name__ == "__main__":
    main()
