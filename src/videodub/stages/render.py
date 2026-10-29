"""配音合成：TTS → 时间轴对齐 → 铺轨 → 混音 → 封装。

关于「顺手消灭 172MB 中间文件」：旧实现先把 voice.wav 用 loudnorm 转成
``chinese_voice_norm.wav``（172MB）再混音，等于多一次全片解码 + 多一份大文件。
现在把 ``loudnorm`` 直接放进封装时的 filtergraph，语义完全一样，中间文件少一个。

成片永远 ``-c:v copy``：**绝不对画面重新编码**。用户拖进来的可能是 4K 或 HEVC，
重编码既慢又损画质，而这件事跟「换配音」没有关系。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from ..errors import MediaError, StageError
from ..log import log
from ..media.audio import SR, decode_f32, fade, peak_normalize, trim_silence, write_wav16
from ..media.ffmpeg import find_ffmpeg, find_ffprobe, run_ffmpeg, to_m4a
from ..progress import Reporter
from .align import plan_placement
from .tts import synthesize_all, write_report

STAGE = "render"
GUARD = 0.06          # 句间留缝：避免两句听起来连成一句
LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11"


def load_items(workdir: Path) -> tuple[list[dict], float]:
    """读 dialogue.json（或退回 translation.json + transcript.json 旧格式）。"""
    total = 0.0
    tj = workdir / "transcript.json"
    if tj.exists():
        total = float(json.loads(tj.read_text(encoding="utf-8")).get("duration") or 0.0)

    dlg = workdir / "dialogue.json"
    if dlg.exists():
        raw = json.loads(dlg.read_text(encoding="utf-8"))
        items = [{
            "id": str(r.get("id", i)),
            "speaker": str(r.get("speaker", "A")).upper()[:1] or "A",
            "start": float(r["start"]),
            "end": float(r.get("end", r["start"])),
            "zh": (r.get("zh") or "").strip(),
        } for i, r in enumerate(raw)]
        items.sort(key=lambda x: x["start"])
        return items, total

    trans = workdir / "translation.json"
    if trans.exists() and tj.exists():
        table = json.loads(trans.read_text(encoding="utf-8"))
        sents = json.loads(tj.read_text(encoding="utf-8"))["sentences"]
        items = [{
            "id": str(s["id"]), "speaker": "A",
            "start": float(s["start"]), "end": float(s["end"]),
            "zh": (table.get(str(s["id"])) or "").strip(),
        } for s in sents]
        return items, total

    raise StageError(
        stage=STAGE, message="缺少 dialogue.json",
        hint="先跑翻译阶段；如果只想转写不配音，不需要这一步",
    )


def build_voice_track(
    items: list[dict],
    *,
    workdir: Path,
    settings,
    total: float,
    reporter: Reporter | None = None,
    bgm: Path | None = None,
) -> tuple[Path, dict]:
    """合成并铺成一条完整中文音轨，返回 ``(wav 路径, 统计)``。"""
    items = [it for it in items if it.get("zh")]
    if not items:
        raise StageError(stage=STAGE, message="没有任何可合成的句子")

    t0 = time.time()
    packs = synthesize_all(
        items,
        voice=settings.voice,
        rate=settings.rate,
        cache_dir=workdir / "cache",
        concurrency=settings.tts_concurrency,
        reporter=reporter,
    )

    if reporter:
        reporter.progress(STAGE, 58, message=f"解码 {len(packs)} 段语音")
        reporter.log("对齐时间轴")

    # 先解码一遍拿到每句真实时长（裁掉首尾静音后的），后面只在需要压缩时重解
    clips: list[np.ndarray] = []
    durations: list[float] = []
    aligned_items: list[dict] = []
    for p in packs:
        base = trim_silence(decode_f32(p.mp3))
        if base.size / SR <= 0.02:
            continue
        clips.append(base)
        durations.append(base.size / SR)
        aligned_items.append({"start": float(items[p.index]["start"]),
                              "end": float(items[p.index]["end"]),
                              "speaker": p.speaker, "zh": p.text})

    if not aligned_items:
        raise StageError(stage=STAGE, message="解码后没有有效语音",
                         hint="检查 TTS 是否正常（videodub doctor）")

    placements, stats = plan_placement(
        aligned_items, durations, total,
        offset=settings.offset,
        max_tempo=settings.max_tempo,
        guard=GUARD,
    )

    # 需要压缩的句子重新解码（atempo 在解码时做，比 numpy 重采样自然）
    if reporter:
        reporter.progress(STAGE, 72, message="铺设音轨")
    need_tempo = [i for i, pl in enumerate(placements) if pl.compressed]
    for i in range(len(clips)):
        if placements[i].compressed:
            clips[i] = trim_silence(decode_f32(packs[i].mp3, placements[i].tempo))
        clips[i] = fade(clips[i])

    canvas_len = int((max(total, stats.final_cursor) + 2.0) * SR)
    canvas = np.zeros(canvas_len, dtype=np.float32)
    for pl, clip in zip(placements, clips):
        i0 = int(pl.start_at * SR)
        i1 = min(i0 + clip.size, canvas_len)
        if i1 > i0 and i0 >= 0:
            canvas[i0:i1] += clip[: i1 - i0]

    canvas = peak_normalize(canvas, settings.peak)
    wav = workdir / "tmp" / "chinese_voice.wav"
    write_wav16(wav, canvas)

    stats_dict = stats.to_dict()
    stats_dict["elapsed_synth"] = round(time.time() - t0, 1)
    stats_dict["voice"] = settings.voice
    stats_dict["rate"] = settings.rate
    stats_dict["max_tempo"] = settings.max_tempo
    stats_dict["offset"] = settings.offset
    stats_dict["tempo_compressed"] = len(need_tempo)
    stats_dict["audio_duration"] = round(canvas.size / SR, 2)

    if reporter:
        reporter.log(
            f"铺入 {stats.placed} 句；压缩 {len(need_tempo)} 句"
            f"{'（均 %.2fx）' % (sum(placements[i].tempo for i in need_tempo) / len(need_tempo)) if need_tempo else ''}"
            f"；最大漂移 {stats.drift_max:.2f} 秒"
        )
    return wav, stats_dict


def mux(
    video: Path,
    voice_wav: Path,
    dst: Path,
    *,
    bgm: Path | None = None,
    bgm_gain: float = 1.0,
) -> None:
    """原画面 + 中文人声（+ 可选伴奏），封装成 mp4。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    aformat = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

    if bgm is not None and bgm.exists():
        fc = (
            f"[2:a]{aformat},volume={bgm_gain:.3f}[bg];"
            f"[1:a]{aformat},{LOUDNESS}[vo];"
            "[vo]asplit=2[vo1][vo2];"
            "[bg][vo2]sidechaincompress=threshold=0.04:ratio=6:attack=12:release=320[duck];"
            "[duck][vo1]amix=inputs=2:normalize=0,alimiter=limit=0.94:level=false[a]"
        )
        args = ["-y", "-v", "error", "-i", str(video), "-i", str(voice_wav),
                "-i", str(bgm), "-filter_complex", fc,
                "-map", "0:v:0", "-map", "[a]"]
    else:
        fc = f"[1:a]{aformat},{LOUDNESS}[vo]"
        args = ["-y", "-v", "error", "-i", str(video), "-i", str(voice_wav),
                "-filter_complex", fc,
                "-map", "0:v:0", "-map", "[vo]"]

    # -dn 去掉源文件里的数据轨（YouTube 的 AV1 文件常带一条 bin_data，
    # 混进 mp4 会让部分播放器声音错位）；-ac 2 统一立体声输出
    args += [
        "-dn", "-map_chapters", "-1",
        "-ac", "2", "-ar", "48000",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest", str(dst),
    ]
    run_ffmpeg(args, stage=STAGE)
    if not dst.exists() or dst.stat().st_size < 1024:
        raise MediaError("封装后没有生成有效视频", hint="检查磁盘空间是否充足")


def render(
    video: Path,
    workdir: Path,
    settings,
    reporter: Reporter | None = None,
    *,
    bgm: Path | None = None,
) -> dict:
    """完整配音流程，返回结果字典（含产物路径与统计）。"""
    items, total = load_items(workdir)
    if total <= 0:
        from ..media.ffmpeg import duration_of

        total = duration_of(video)

    wav, stats = build_voice_track(
        items, workdir=workdir, settings=settings, total=total,
        reporter=reporter, bgm=bgm,
    )

    if reporter:
        reporter.progress(STAGE, 88,
                          message=f"封装成片（原画面 + 中文人声{' + 原伴奏' if bgm else ''}）")

    final = workdir / "dubbed.mp4"
    mux(video, wav, final, bgm=bgm, bgm_gain=settings.bgm_gain)

    audio_only = workdir / "dubbed_audio.m4a"
    try:
        to_m4a(final, audio_only)
    except MediaError:
        audio_only = Path()

    payload = {
        **stats,
        "bgm": bool(bgm and Path(bgm).exists()),
        "placed": stats.get("placed", 0),
    }
    write_report(workdir, payload)

    if reporter:
        reporter.progress(STAGE, 100,
                          message=f"成片 {final.stat().st_size / 1024 / 1024:.0f} MB")

    return {
        "video": str(final),
        "audio": str(audio_only) if audio_only and audio_only.exists() else "",
        "report": payload,
        "duration": total,
    }


def probe_playable(video: Path) -> dict:
    """给界面用：判断浏览器能不能直接播这个成片。"""
    try:
        if find_ffprobe():
            from ..media.ffmpeg import probe

            return probe(video)
    except Exception:  # noqa: BLE001
        pass
    return {"playable": True, "duration": 0.0}


__all__ = ["STAGE", "GUARD", "build_voice_track", "load_items", "mux", "render",
           "probe_playable", "find_ffmpeg", "log"]
