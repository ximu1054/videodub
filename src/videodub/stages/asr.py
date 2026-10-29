"""语音识别（faster-whisper）。

三个要点：
  - **GPU 优先，失败自动退 CPU**。退的时候要打印原因，否则用户只看到「变慢了」
    却不知道为什么。
  - **整段 wav 读进内存**再传给模型。这是为了绕开不同版本 PyAV 的接口差异，
    代价是长视频占内存（1 小时 16k 单声道约 115MB，可接受）。
  - **离线/镜像自动切换**：模型已缓存就 ``HF_HUB_OFFLINE=1``，
    没缓存才按配置决定直连还是走镜像。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings, hf_env_for_model
from ..errors import MissingDependencyError, StageError
from ..log import log
from ..media.audio import read_wav_16k
from ..media.ffmpeg import duration_of, extract_audio_16k, probe
from ..progress import Reporter
from ..util import fmt_ts
from .. import platforms
from .segment import to_sentences

STAGE = "asr"
MODEL_REPOS = "Systran/faster-whisper-{model}"

MODEL_SIZES = {
    "tiny": 0.08, "base": 0.15, "small": 0.5,
    "medium": 1.5, "large-v3": 2.9, "large-v3-turbo": 1.6,
}


def _load_model(model_name: str, device: str, reporter: Reporter | None):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MissingDependencyError(
            "缺少 faster-whisper",
            hint="pip install faster-whisper",
            detail=str(exc),
        ) from exc

    if device in ("auto", "cuda"):
        added = platforms.current().enable_cuda_dlls()
        try:
            m = WhisperModel(model_name, device="cuda", compute_type="float16")
            return m, "cuda", "float16"
        except Exception as exc:  # noqa: BLE001
            if device == "cuda":
                raise StageError(
                    stage=STAGE,
                    message=f"无法在 GPU 上加载 Whisper（{type(exc).__name__}）",
                    hint="检查显卡驱动与 CUDA 版 torch；或把 device 设成 cpu",
                    detail=str(exc)[:800],
                ) from exc
            msg = f"GPU 不可用（{type(exc).__name__}: {str(exc)[:80]}），改用 CPU"
            if reporter:
                reporter.log(msg, "warn")
            else:
                log(msg, "warn")
            if added and reporter:
                reporter.log(f"已尝试挂载 CUDA 运行库：{len(added)} 个目录", "debug")

    return WhisperModel(model_name, device="cpu", compute_type="int8"), "cpu", "int8"


# CTranslate2 在 Windows 上是**懒加载** DLL 的：``WhisperModel(...)`` 能成功构造，
# 真正推理时才抛「Library cublas64_12.dll is not found」。所以只包住构造是不够的，
# 必须把首次推理也一起包住。这个坑端到端跑一次就会撞上。
_CUDA_DLL_MARKERS = ("cublas", "cudnn", "cudart", "is not found or cannot be loaded")

CUDA_DLL_HINT = (
    "faster-whisper 需要 CUDA 运行库（cuBLAS / cuDNN），装了显卡驱动并不包含它们。\n"
    "装进虚拟环境最简单：pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 "
    "nvidia-cuda-runtime-cu12\n"
    "或者直接用 CPU：videodub run <目标> --device cpu（约慢 3~4 倍）"
)


def _looks_like_cuda_dll_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _CUDA_DLL_MARKERS)


def _collect(model, audio, settings, reporter, total: float) -> tuple[list, object]:
    """跑一遍识别并把所有词收集起来。异常按原样抛出，由调用方决定怎么解释。"""
    segments, info = model.transcribe(
        audio,
        language=settings.asr_language or None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 350},
        word_timestamps=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    if reporter:
        reporter.log(f"识别到语言：{info.language}（置信度 {info.language_probability:.2f}）")

    words = []
    for seg in segments:
        if seg.words:
            words.extend(seg.words)
        if reporter and total > 0 and seg.end:
            pct = min(98.0, 5 + seg.end / total * 93.0)
            reporter.progress(
                STAGE, pct,
                message=f"识别到 {fmt_ts(seg.end)} / {fmt_ts(total)}",
                metrics={"audio_pos": round(seg.end, 1), "duration": round(total, 1)},
            )
    return words, info


def transcribe(
    video: Path,
    workdir: Path,
    settings: Settings,
    reporter: Reporter | None = None,
) -> dict:
    """识别 → 写 ``transcript.json`` / ``transcript.txt``。"""
    workdir.mkdir(parents=True, exist_ok=True)
    wav = workdir / "tmp" / "audio16k.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)

    if reporter:
        reporter.progress(STAGE, 2, message="抽取音轨")

    # 先拿时长，用于进度百分比
    try:
        total = probe(video).get("duration") or 0.0
    except Exception:  # noqa: BLE001
        total = 0.0

    extract_audio_16k(video, wav)
    if total <= 0:
        total = duration_of(wav)

    hf_env_for_model(MODEL_REPOS.format(model=settings.asr_model), settings.hf_mirror)
    if reporter:
        reporter.progress(STAGE, 5, message=f"加载 Whisper {settings.asr_model}")

    model, device, compute = _load_model(settings.asr_model, settings.asr_device, reporter)
    if reporter:
        reporter.log(f"运行设备：{device}/{compute}")

    audio = read_wav_16k(wav)
    try:
        words, info = _collect(model, audio, settings, reporter, total)
    except RuntimeError as exc:
        if not (_looks_like_cuda_dll_error(exc) and device == "cuda"):
            raise
        if settings.asr_device == "cuda":
            raise StageError(
                stage=STAGE,
                message="缺少 CUDA 运行库，无法在 GPU 上识别",
                hint=CUDA_DLL_HINT,
                detail=str(exc)[:800],
            ) from exc
        # device=auto：不因为缺两个 DLL 就让用户卡住，退回 CPU 并大声说清楚
        msg = "GPU 缺少 CUDA 运行库，已自动改用 CPU（会慢 3~4 倍）"
        if reporter:
            reporter.log(msg, "warn")
            reporter.log(CUDA_DLL_HINT, "debug")
        else:
            log(msg, "warn")
        del model
        from faster_whisper import WhisperModel

        model = WhisperModel(settings.asr_model, device="cpu", compute_type="int8")
        device, compute = "cpu", "int8"
        words, info = _collect(model, audio, settings, reporter, total)

    if not words:
        raise StageError(
            stage=STAGE, message="没有识别到任何语音",
            hint="确认视频有声音轨；如果是纯音乐视频，这个工具不适用",
        )

    sents = to_sentences(words)
    for i, s in enumerate(sents):
        s["id"] = i

    payload = {
        "source": str(video),
        "duration": round(duration_of(wav), 3),
        "language": info.language,
        "sentences": sents,
    }
    (workdir / "transcript.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    _write_txt(workdir / "transcript.txt", video.name, sents, payload["duration"])

    if reporter:
        reporter.log(f"识别到 {len(sents)} 句，总长 {fmt_ts(payload['duration'])}")
        reporter.progress(STAGE, 100, message=f"{len(sents)} 句")
    return payload


def _write_txt(path: Path, name: str, sents: list[dict], duration: float) -> None:
    lines = [f"# {name}  |  {len(sents)} 句  |  总长 {fmt_ts(duration)}", ""]
    for s in sents:
        lines.append(f"[{s['id']}] {fmt_ts(s['start'])} -> {fmt_ts(s['end'])}")
        lines.append(f"    {s['text']}")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_transcript(workdir: Path) -> dict:
    p = workdir / "transcript.json"
    if not p.exists():
        raise StageError(stage=STAGE, message="缺少 transcript.json", hint="先跑识别阶段")
    return json.loads(p.read_text(encoding="utf-8"))


def model_cached(model_name: str) -> bool:
    from ..config import model_cached as cached

    return cached(MODEL_REPOS.format(model=model_name))
