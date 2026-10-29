"""语音合成（Edge-TTS）。

**这是整条链路里唯一必须联网的环节**。ASR 与翻译都在本机跑，只有译文文本
会发给微软的朗读接口。README 里必须说清楚这一点，否则「完全离线」是个假承诺。

两个保命机制（沿用旧版，都很有效）：
  - **哈希缓存**：``sha1(voice|rate|text)[:16]``。改一句译文只重合成那一句，
    长视频重跑从 3 分钟降到 12 秒。
  - **失败重试 3 次 + 退避**：微软接口偶尔抽风，单句失败不该毁掉整条音轨。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import NetworkError
from ..log import log
from ..progress import Reporter

STAGE = "render"
MAX_BYTES_MIN = 500      # 小于这个字节数认为合成失败（空文件或纯元数据）


@dataclass
class Synthesis:
    index: int
    text: str
    mp3: Path
    speaker: str
    voice: str


class TTSBackend(Protocol):
    """替换 TTS 引擎只需实现这个协议。

    想换成自建引擎 / 商业 TTS，改这里，不要动 render.py。
    """

    async def synth(self, text: str, voice: str, rate: str, dst: Path) -> None: ...


class EdgeBackend:
    """微软 Edge 朗读接口（免费，但服务条款对商用有限制）。"""

    name = "edge"

    async def synth(self, text: str, voice: str, rate: str, dst: Path) -> None:
        import edge_tts

        await edge_tts.Communicate(text, voice, rate=rate).save(str(dst))


def cache_key(voice: str, rate: str, text: str) -> str:
    return hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:16]


async def _synth_one(
    text: str, voice: str, rate: str, dst: Path,
    sem: asyncio.Semaphore, backend: TTSBackend, tries: int = 3,
) -> bool:
    async with sem:
        for attempt in range(tries):
            try:
                await backend.synth(text, voice, rate, dst)
                if dst.exists() and dst.stat().st_size > MAX_BYTES_MIN:
                    return True
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    log(f"合成失败：{type(exc).__name__}: {str(exc)[:80]} | {text[:20]}", "warn")
                    return False
                await asyncio.sleep(1.5 * (attempt + 1))
    return False


def synthesize_all(
    items: list[dict],
    *,
    voice: str,
    rate: str,
    cache_dir: Path,
    concurrency: int = 4,
    reporter: Reporter | None = None,
    backend: TTSBackend | None = None,
) -> list[Synthesis]:
    """合成全部条目，返回成功的部分。

    ``items`` 里每条需要 ``zh``（译文），可选的 ``speaker`` / ``_index``。
    """
    backend = backend or EdgeBackend()
    cache_dir.mkdir(parents=True, exist_ok=True)

    pending: list[tuple[int, dict, str, Path]] = []
    hit = 0
    for idx, it in enumerate(items):
        text = (it.get("zh") or "").strip()
        if not text:
            continue
        spk = str(it.get("speaker", "A")).upper()[:1] or "A"
        mp3 = cache_dir / f"{cache_key(voice, rate, text)}.mp3"
        if mp3.exists():
            hit += 1
            continue
        pending.append((idx, it, text, mp3))

    if reporter:
        if pending:
            reporter.log(f"合成 {len(pending)} 句（{rate}），缓存命中 {hit} 句")
        else:
            reporter.log(f"全部命中缓存（{hit} 句），跳过合成")

    if pending:
        asyncio.run(_run_batch(pending, voice, rate, concurrency, backend, reporter))

    out: list[Synthesis] = []
    for idx, it in enumerate(items):
        text = (it.get("zh") or "").strip()
        if not text:
            continue
        mp3 = cache_dir / f"{cache_key(voice, rate, text)}.mp3"
        if mp3.exists() and mp3.stat().st_size > MAX_BYTES_MIN:
            out.append(Synthesis(
                index=idx, text=text, mp3=mp3,
                speaker=str(it.get("speaker", "A")).upper()[:1] or "A", voice=voice,
            ))
    if not out:
        raise NetworkError(
            "所有句子都合成失败",
            hint="Edge-TTS 需要联网。检查网络能否访问 speech.platform.bing.com，"
                 "或换一个 TTS 引擎（stages/tts.py 的 TTSBackend）",
        )
    return out


async def _run_batch(pending, voice, rate, concurrency, backend, reporter) -> None:
    sem = asyncio.Semaphore(max(1, concurrency))
    tasks = [
        asyncio.create_task(_synth_one(text, voice, rate, mp3, sem, backend))
        for _, _, text, mp3 in pending
    ]
    done = 0
    total = len(tasks)
    for coro in asyncio.as_completed(tasks):
        await coro
        done += 1
        if reporter and (done % 10 == 0 or done == total):
            reporter.progress(
                STAGE, done / total * 55.0,   # 合成占 render 阶段前 55%
                message=f"合成语音 {done}/{total}",
                metrics={"synth_done": done, "synth_total": total},
            )


# ---------------------------------------------------------------- 试听
def render_preview(text: str, voice: str, rate: str, dst: Path) -> Path:
    """合成一段试听音频（给界面的音色卡片用）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > MAX_BYTES_MIN:
        return dst
    backend = EdgeBackend()
    asyncio.run(backend.synth(text, voice, rate, dst))
    if not dst.exists() or dst.stat().st_size <= MAX_BYTES_MIN:
        raise NetworkError("试听合成失败", hint="试听需要联网（微软朗读接口）")
    return dst


def write_report(workdir: Path, payload: dict) -> Path:
    p = workdir / "render_report.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
