"""检测画面里内嵌字幕的出现区间，用来和中文音轨对时。

原理：字幕是白字黑边，落在画面下方。把下方区域转灰度后统计高亮像素比例，
比例跨过阈值就当成"有字幕"，取连续区间。

用法：python scripts/subsync.py --video <视频> --out out [--fps 10]
产物：out/subtitles.json  [{"start": .., "end": ..}, ...]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, ffmpeg  # noqa: E402

W, H = 320, 72      # 抽样分辨率（只看下方字幕带）
TOP = 0.63          # 字幕带起点（避开 B 站水印和幻灯片底部内容）
BAND = 0.20         # 字幕带高度


def edge_density(video: Path, fps: float) -> np.ndarray:
    """字幕是白字黑描边，横向梯度很高；纯幻灯片画面梯度低得多。"""
    vf = (f"crop=iw:ih*{BAND:.3f}:0:ih*{TOP:.3f},scale={W}:{H},format=gray")
    cmd = [ffmpeg(), "-v", "error", "-i", str(video), "-an", "-vf", vf,
           "-r", str(fps), "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8)
    n = len(frames) // (W * H)
    g = frames[: n * W * H].reshape(n, H, W).astype(np.float32)
    return np.abs(np.diff(g, axis=2)).mean(axis=(1, 2))


def find_intervals(ratio: np.ndarray, fps: float, thresh: float,
                   min_gap: float = 0.25, min_len: float = 0.4):
    on = ratio > thresh
    # 合并短空隙，去掉过短片段
    intervals, i, n = [], 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j + 1 < n and on[j + 1]:
                j += 1
            intervals.append([i / fps, (j + 1) / fps])
            i = j + 1
        else:
            i += 1
    merged = []
    for s, e in intervals:
        if merged and s - merged[-1][1] < min_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(round(s, 2), round(e, 2)) for s, e in merged if e - s >= min_len]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--fps", type=float, default=10.0)
    args = ap.parse_args()

    out = Path(args.out)
    ratio = edge_density(Path(args.video), args.fps)
    print(f"采样 {len(ratio)} 帧，下方区域边缘密度 中位数 {np.median(ratio):.2f} "
          f"最大 {ratio.max():.2f}")

    # 阈值取"无字幕水平"和"有字幕水平"的中间
    lo = float(np.percentile(ratio, 10))
    hi = float(np.percentile(ratio, 80))
    thresh = lo + (hi - lo) * 0.45
    subs = find_intervals(ratio, args.fps, thresh)
    print(f"阈值 {thresh:.2f}（底噪 {lo:.2f} / 字幕 {hi:.2f}）→ 检出 {len(subs)} 段字幕")

    (out / "subtitles.json").write_text(
        json.dumps([{"start": s, "end": e} for s, e in subs], indent=1), encoding="utf-8")

    items_path = out / "dialogue.json"
    if items_path.exists():
        items = json.loads(items_path.read_text(encoding="utf-8"))
        print("\n内嵌字幕起始 vs 中文音轨起始（取最近的一条）：")
        diffs = []
        for it in items:
            st = float(it["start"])
            near = min(subs, key=lambda x: abs(x[0] - st)) if subs else None
            if near and abs(near[0] - st) < 3.0:
                d = near[0] - st
                diffs.append(d)
                print(f"  句 {str(it['id']):>3} 音轨 {st:7.2f}  字幕 {near[0]:7.2f}  差 {d:+.2f}s")
        if diffs:
            print(f"\n平均差 {np.mean(diffs):+.2f}s  中位数 {np.median(diffs):+.2f}s  "
                  f"范围 {min(diffs):+.2f} ~ {max(diffs):+.2f}")


if __name__ == "__main__":
    main()
