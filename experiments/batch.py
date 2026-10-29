"""批量处理一个文件夹里的所有视频。

每个视频独立一个输出目录（out_batch/<视频名>/），互不覆盖。
分三态推进，可以随时中断、重跑：

  1. 没 transcript.json      → 跑识别
  2. 有 transcript.json      → 报告"待翻译"（需要人/AI 写 dialogue.json）
  3. 有 dialogue.json        → 跑合成，出 dubbed.mp4

用法：
  python scripts/batch.py --dir "D:/videos"                 # 推进到能做的最远一步
  python scripts/batch.py --dir "D:/videos" --list          # 只看状态，不干活
  python scripts/batch.py --dir "D:/videos" --force render  # 强制重做合成
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg"}
PY = sys.executable
SCRIPTS = Path(__file__).resolve().parent


def slugify(name: str) -> str:
    """视频名 → 目录名。保留中文，只清掉 Windows 不接受的字符。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return s[:60] or "video"


def find_videos(root: Path, recursive: bool) -> list[Path]:
    it = root.rglob("*") if recursive else root.glob("*")
    files = [p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    return sorted(files, key=lambda p: str(p).lower())


def build_index(videos: list[Path], workroot: Path) -> dict[Path, Path]:
    """视频 → 输出目录。同名不同目录时加短哈希，避免互相覆盖。"""
    seen: dict[str, int] = {}
    mapping: dict[Path, Path] = {}
    for v in videos:
        base = slugify(v.stem)
        if base in seen:
            seen[base] += 1
            tag = hashlib.sha1(str(v).encode()).hexdigest()[:6]
            base = f"{base}_{tag}"
        else:
            seen[base] = 0
        mapping[v] = workroot / base
    return mapping


def status(outdir: Path) -> tuple[str, str]:
    """返回 (阶段, 说明)"""
    if not (outdir / "transcript.json").exists():
        return "need_prep", "未识别"
    if not (outdir / "dialogue.json").exists():
        return "need_translate", "待翻译"
    if not (outdir / "dubbed.mp4").exists():
        return "need_render", "待合成"
    return "done", "已完成"


def run(cmd: list[str], title: str) -> int:
    print(f"\n$ {title}")
    p = subprocess.run(cmd, cwd=str(ROOT))
    return p.returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="视频所在文件夹")
    ap.add_argument("--work", default=None, help="输出根目录，默认 dub/out_batch")
    ap.add_argument("--list", action="store_true", help="只列状态")
    ap.add_argument("--force", default=None, choices=["prep", "translate", "render"],
                    help="强制重做某一步")
    ap.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    # 合成参数，默认值与当前定稿一致
    ap.add_argument("--voice-a", default="zh-CN-XiaoxiaoNeural")
    ap.add_argument("--voice-b", default="zh-CN-XiaoyiNeural")
    ap.add_argument("--rate", default="+10%")
    ap.add_argument("--max-tempo", type=float, default=1.18)
    ap.add_argument("--peak", type=float, default=0.89)
    ap.add_argument("--offset", type=float, default=0.0,
                    help="整条音轨平移（负值=提前）。默认 0，按素材单独定")
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"目录不存在：{root}")
    workroot = Path(args.work) if args.work else ROOT / "out_batch"
    workroot.mkdir(parents=True, exist_ok=True)

    videos = find_videos(root, not args.no_recursive)
    if not videos:
        raise SystemExit(f"在 {root} 里没找到视频（支持 {' '.join(sorted(VIDEO_EXT))}）")

    mapping = build_index(videos, workroot)

    print(f"目录：{root}")
    print(f"找到 {len(videos)} 个视频，输出到 {workroot}\n")
    counts: dict[str, int] = {}
    todo: list[tuple[Path, Path, str]] = []
    for v in videos:
        outdir = mapping[v]
        st, label = status(outdir)
        counts[st] = counts.get(st, 0) + 1
        todo.append((v, outdir, st))
        size = v.stat().st_size / 1024 / 1024
        print(f"  [{label:<4}] {size:7.1f}M  {v.name}")
        if st != "done":
            print(f"           → {outdir}")

    print("\n汇总：" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.list:
        return

    # 逐个推进
    for v, outdir, st in todo:
        outdir.mkdir(parents=True, exist_ok=True)
        if st == "need_prep" or args.force == "prep":
            print(f"\n{'='*60}\n识别：{v.name}\n{'='*60}")
            rc = run([PY, str(SCRIPTS / "prep.py"), str(v), "--out", str(outdir)], "prep")
            if rc != 0:
                print(f"!! 识别失败（退出码 {rc}），跳过：{v.name}")
                continue
            st = status(outdir)[0]

        if st == "need_translate":
            print(f"\n[{v.name}] 已识别，等翻译。")
            print(f"  原文：{outdir / 'transcript.txt'}")
            print(f"  写好后放到：{outdir / 'dialogue.json'}")
            continue

        if st == "need_render" or args.force == "render":
            print(f"\n{'='*60}\n合成：{v.name}\n{'='*60}")
            cmd = [PY, str(SCRIPTS / "render.py"), "--out", str(outdir), "--video", str(v),
                   "--voice-a", args.voice_a, "--voice-b", args.voice_b,
                   "--rate", args.rate, "--max-tempo", str(args.max_tempo),
                   "--peak", str(args.peak), "--offset", str(args.offset)]
            rc = run(cmd, "render")
            if rc != 0:
                print(f"!! 合成失败（退出码 {rc}）：{v.name}")

    # 收尾报告
    print(f"\n{'='*60}\n最终状态\n{'='*60}")
    need = []
    for v, outdir, _ in todo:
        st, label = status(outdir)
        print(f"  [{label:<4}] {v.name}")
        if st == "need_translate":
            need.append((v, outdir))
    if need:
        print(f"\n还有 {len(need)} 个视频等待翻译：")
        for v, outdir in need:
            print(f"  {v.name}\n    原文 {outdir / 'transcript.txt'}\n    目标 {outdir / 'dialogue.json'}")
        sys.exit(2)


if __name__ == "__main__":
    main()
