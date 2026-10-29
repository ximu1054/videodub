"""按句判断说话人：用音高 + 频谱包络做两聚类，给每句打上说话人标记。

用法：python scripts/speakers.py --out out
产物：out/speakers.json   {"0": "A", "1": "A", ...}
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

SR = 16000
FRAME = 512
HOP = 160
F0_MIN, F0_MAX = 70.0, 340.0


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        assert f.getframerate() == SR, f.getframerate()
        a = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")
    return a.astype(np.float32) / 32768.0


def frame_feats(seg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (每帧 F0, 每帧对数频谱包络)。"""
    n = 1 + max(0, (len(seg) - FRAME) // HOP)
    if n <= 0:
        return np.zeros(0), np.zeros((0, 1))
    win = np.hanning(FRAME).astype(np.float32)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    frames = seg[idx] * win
    rms = np.sqrt((frames**2).mean(axis=1))

    spec = np.abs(np.fft.rfft(frames, axis=1)) + 1e-8
    freqs = np.fft.rfftfreq(FRAME, 1 / SR)

    # 24 个对数间隔频带（80Hz ~ 6kHz），做语音音色的粗指纹
    edges = np.geomspace(80, 6000, 25)
    env = np.zeros((n, 24), dtype=np.float32)
    for i in range(24):
        m = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if m.any():
            env[:, i] = np.log(spec[:, m].mean(axis=1))

    # 自相关估基频
    lag_min, lag_max = int(SR / F0_MAX), int(SR / F0_MIN)
    f0 = np.zeros(n, dtype=np.float32)
    for i in range(n):
        if rms[i] < 1e-4:
            continue
        x = frames[i] - frames[i].mean()
        ac = np.correlate(x, x, mode="full")[FRAME - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        lo, hi = lag_min, min(lag_max, len(ac) - 1)
        if hi <= lo:
            continue
        k = int(np.argmax(ac[lo:hi])) + lo
        if ac[k] > 0.3:
            f0[i] = SR / k
    return f0, env


def kmeans(x: np.ndarray, k: int = 2, iters: int = 60, seed: int = 0):
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(24):
        c = x[rng.choice(len(x), k, replace=False)].copy()
        for _ in range(iters):
            d = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
            lab = d.argmin(axis=1)
            new = np.stack([x[lab == j].mean(axis=0) if (lab == j).any() else c[j]
                            for j in range(k)])
            if np.allclose(new, c):
                break
            c = new
        inertia = ((x - c[lab]) ** 2).sum()
        if best is None or inertia < best[0]:
            best = (inertia, lab.copy(), c.copy())
    return best[1], best[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--wav", default=None)
    args = ap.parse_args()

    out = Path(args.out)
    wav = Path(args.wav) if args.wav else out / "audio16k.wav"
    audio = load(wav)
    data = json.loads((out / "transcript.json").read_text(encoding="utf-8"))

    rows, feats = [], []
    for s in data["sentences"]:
        a = max(0, int(float(s["start"]) * SR))
        b = min(len(audio), int(float(s["end"]) * SR))
        seg = audio[a:b]
        if len(seg) < FRAME * 2:
            rows.append((s["id"], 0.0, 0.0))
            feats.append(np.zeros(25, dtype=np.float32))
            continue
        f0, env = frame_feats(seg)
        voiced = f0 > 0
        f0m = float(np.median(f0[voiced])) if voiced.any() else 0.0
        e = env[voiced].mean(axis=0) if voiced.any() else env.mean(axis=0)
        rows.append((s["id"], f0m, float(len(seg) / SR)))
        feats.append(np.concatenate([[f0m], e]))

    X = np.stack(feats)
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6
    Z = (X - mu) / sd
    # 音高权重加倍，它是区分同性别人声最稳的线索
    Z[:, 0] *= 2.0
    lab, cent = kmeans(Z)

    print(f"{'句':>4} {'时长':>6} {'基频Hz':>7} 簇")
    for (sid, f0m, dur), l in zip(rows, lab):
        print(f"{sid:>4} {dur:>6.2f} {f0m:>7.1f}  {l}")
    print(f"\n簇中心基频: {[round(float(cent[j][0] * sd[0] + mu[0]), 1) for j in range(2)]}")

    mapping = {"A": "A", "B": "B"}
    res = {str(sid): ("A" if l == 0 else "B") for (sid, _, _), l in zip(rows, lab)}
    (out / "speakers.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print("已写入", out / "speakers.json", mapping)


if __name__ == "__main__":
    main()
