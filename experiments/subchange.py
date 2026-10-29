"""检测内嵌字幕的换行时刻，用来给中文音轨对时。

思路：字幕换行时，字幕带内的像素会突然变化（文字整体换掉），
而说话人说话、镜头轻微抖动在这些像素上的变化小得多。
用「字幕带帧间差分」的尖峰当换行点，比判断"有没有字幕"稳。

用法：python scripts/subchange.py --video <视频> --out out [--fps 20]
产物：out/subchanges.json  {"changes": [...], "lines": [[s,e], ...]}
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

W, H = 320, 72
TOP, BAND = 0.63, 0.20


def band_frames(video: Path, fps: float) -> np.ndarray:
    vf = f"crop=iw:ih*{BAND:.3f}:0:ih*{TOP:.3f},scale={W}:{H},format=gray"
    raw = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", str(video), "-an", "-vf", vf,
         "-r", str(fps), "-f", "rawvideo", "-"],
        check=True, capture_output=True).stdout
    f = np.frombuffer(raw, dtype=np.uint8)
    n = len(f) // (W * H)
    return f[: n * W * H].reshape(n, H, W).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--fps", type=float, default=20.0)
    ap.add_argument("--sensitivity", type=float, default=1.0)
    args = ap.parse_args()

    out = Path(args.out)
    frames = band_frames(Path(args.video), args.fps)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    print(f"{len(frames)} 帧 @ {args.fps}fps，字幕带差分：中位数 {np.median(diff):.2f} "
          f"p99 {np.percentile(diff, 99):.2f} 最大 {diff.max():.2f}")

    med = float(np.median(diff))
    mad = float(np.median(np.abs(diff - med))) + 1e-6
    thr = med + max(4.0 * mad, 3.0) * args.sensitivity
    peaks = np.where(diff > thr)[0]
    print(f"阈值 {thr:.2f} → 原始尖峰 {len(peaks)} 个")

    # 抑制相邻帧的重复触发：0.4s 内只保留最强的那个
    guard = int(0.4 * args.fps)
    picked: list[int] = []
    for i in peaks:
        if picked and i - picked[-1] < guard:
            if diff[i] > diff[picked[-1]]:
                picked[-1] = int(i)
        else:
            picked.append(int(i))
    times = [round((i + 1) / args.fps, 2) for i in picked]
    print(f"去重后 {len(times)} 个换行点，间隔中位数 "
          f"{np.median(np.diff(times)) if len(times) > 1 else 0:.2f}s")

    (out / "subchanges.json").write_text(json.dumps(
        {"fps": args.fps, "threshold": round(thr, 2), "changes": times},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写入", out / "subchanges.json")
    print("换行点（前 20 个）:", times[:20])

    items_path = out / "dialogue.json"
    if items_path.exists() and times:
        items = json.loads(items_path.read_text(encoding="utf-8"))
        starts = [float(i["start"]) for i in items]
        d = []
        for t in times:
            near = min(starts, key=lambda x: abs(x - t))
            if abs(near - t) < 2.5:
                d.append(t - near)
        if d:
            d = np.array(d)
            print(f"\n换行点 − 最近句起点：中位数 {np.median(d):+.2f}s  均值 {d.mean():+.2f}s  "
                  f"标准差 {d.std():.2f}s（{len(d)} 对）")


if __name__ == "__main__":
    main()
