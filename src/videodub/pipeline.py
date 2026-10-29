"""阶段编排：把四个阶段串成一条流水线。

设计原则：**用产物文件判断进度，而不是用状态字段**。
这样即使进程被杀、机器断电，重跑时也能从正确的阶段继续——状态字段可能撒谎，
``dubbed.mp4`` 存在与否不会。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import Settings
from .errors import Cancelled, StageError, UserInputError, VideoDubError
from .log import log
from .media.ffmpeg import duration_of, probe
from .progress import STAGES, Reporter
from .stages import asr, download, render, translate, units as units_mod
from .util import VIDEO_EXT, is_url, normalize_input
from . import platforms


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def build_source(target: str, settings: Settings) -> tuple[dict, Path | None]:
    """把用户输入解析成 source 描述 + 本地路径（URL 时路径稍后才有）。"""
    raw = normalize_input(target)
    if not raw:
        raise UserInputError("没有输入内容", hint="粘贴视频网址，或拖入本地视频文件")

    if is_url(raw):
        cookies = download.find_cookies()
        meta = download.probe_url(raw, settings)
        title = meta.get("title") or download.get_title(raw, settings, cookies) or "video"
        return {
            "kind": "url", "url": raw, "title": title,
            "duration": float(meta.get("duration") or 0.0),
            "uploader": meta.get("uploader", ""),
            "thumbnail": meta.get("thumbnail", ""),
            "playable": True,
        }, None

    p = Path(raw).expanduser().resolve()
    if not p.exists():
        raise UserInputError(f"文件不存在：{p}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise UserInputError(
            f"不支持的格式：{p.suffix}",
            hint="支持 " + "、".join(sorted(VIDEO_EXT)),
        )
    info = probe(p)
    return {
        "kind": "file", "path": str(p), "title": p.stem, "name": p.name,
        "duration": float(info.get("duration") or 0.0),
        "size": p.stat().st_size,
        "vcodec": info.get("vcodec", ""),
        "width": info.get("width", 0), "height": info.get("height", 0),
        "playable": bool(info.get("playable", True)),
    }, p


def sources_from_playlist(info: dict, url: str, *, limit: int = 0) -> list[dict]:
    """把探测结果拆成「一集一个 source」。

    刻意不合并成一个任务：拆开之后，进度、断点续跑、失败重跑、单集删除
    全部沿用现成逻辑，一行都不用改。
    """
    entries = list(info.get("entries") or [])
    if limit and int(limit) > 0:
        entries = entries[:int(limit)]

    total = int(info.get("total") or len(entries))
    sources: list[dict] = []
    for e in entries:
        src = {
            "kind": "url",
            "url": e.get("url") or url,
            "title": e.get("title") or f"第 {e['index']} 集",
            "duration": float(e.get("duration") or 0.0),
            "uploader": info.get("uploader", ""),
            "thumbnail": info.get("thumbnail", ""),
            "playable": True,
            "playlist": {
                "id": info.get("id", ""),
                "title": info.get("title", ""),
                "index": e["index"],
                "total": total,
            },
        }
        if not e.get("url"):
            # 没有直链的站点：退回「列表地址 + 第几集」
            src["playlist_item"] = e["index"]
        sources.append(src)
    return sources


def build_playlist_sources(url: str, settings: Settings, *, limit: int = 0
                           ) -> tuple[list[dict], dict]:
    """探测一个合集链接，返回 ``(每集的 source 列表, 原始探测结果)``。"""
    info = download.probe_playlist(url, settings)
    if not info.get("ok"):
        raise UserInputError(info.get("error") or "合集解析失败")
    if not info.get("is_playlist"):
        raise UserInputError("这个链接不是合集",
                             hint="它只有一个视频，直接点「开始配音」就行")
    if not info.get("entries"):
        raise UserInputError(
            "合集里没有可下载的集数",
            hint="如果需要登录：用浏览器扩展导出 cookies.txt，放进「下载」文件夹再试",
        )
    return sources_from_playlist(info, url, limit=limit), info


def resolve_video(job, settings: Settings, reporter: Reporter | None,
                  cancel: threading.Event | None = None) -> Path:
    """拿到本地视频文件；URL 时下载。"""
    src = job.source
    if src.get("kind") == "file":
        p = Path(src["path"])
        if not p.exists():
            raise UserInputError(f"源文件已被移动或删除：{p}")
        return p

    existing = job.source_video()
    if existing:
        if reporter:
            reporter.stage_done("download", 0.0, f"已下载（{existing.name}）")
        return existing

    t0 = time.time()
    if reporter:
        reporter.stage_start("download", "下载视频")
    video = download.download(src["url"], job.workdir, settings, reporter,
                              item=src.get("playlist_item") or None)
    download.write_source_url(job.workdir, src["url"])
    if not src.get("duration"):
        try:
            src["duration"] = duration_of(video)
        except VideoDubError:
            pass
    if reporter:
        reporter.stage_done("download", time.time() - t0)
    _check(cancel)
    return video


def run_stage_asr(job, video: Path, settings: Settings, reporter: Reporter,
                  cancel: threading.Event | None) -> dict:
    t0 = time.time()
    if reporter:
        reporter.stage_start("asr", f"识别语音（Whisper {settings.asr_model}）")
    payload = asr.transcribe(video, job.workdir, settings, reporter)
    if reporter:
        reporter.stage_done("asr", time.time() - t0, f"{len(payload['sentences'])} 句")
    _check(cancel)
    return payload


def run_stage_translate(job, transcript: dict, settings: Settings, reporter: Reporter,
                        cancel: threading.Event | None) -> list[dict]:
    t0 = time.time()
    sents = transcript.get("sentences") or []
    if not sents:
        raise StageError(stage="translate", message="识别结果里没有句子")

    language = transcript.get("language") or ""
    if translate.skip_reason(language):
        # 原音轨就是中文：不需要翻译，直接用原文当配音稿
        if reporter:
            reporter.stage_start("translate", "原音轨已是中文，跳过翻译")
            items = [{"id": str(s["id"]), "speaker": "A",
                      "start": float(s["start"]), "end": float(s["end"]),
                      "zh": s["text"]} for s in sents]
            translate.write_dialogue(job.workdir, items)
            reporter.stage_done("translate", time.time() - t0, "跳过")
        return items

    unit_list = units_mod.build_units(sents)
    if reporter:
        reporter.stage_start("translate", f"{len(sents)} 句 → {len(unit_list)} 个翻译单元")

    done = translate.translate_units(
        unit_list, workdir=job.workdir, settings=settings,
        context=job.source.get("title", ""), reporter=reporter,
    )
    items = units_mod.units_to_dialogue(unit_list, done)
    translate.write_dialogue(job.workdir, items)

    if reporter:
        ratio = translate.zh_ratio(unit_list, done)
        reporter.stage_done("translate", time.time() - t0,
                            f"{len(items)} 条，中文占比 {ratio:.0%}")
    _check(cancel)
    return items


def run_stage_render(job, video: Path, settings: Settings, reporter: Reporter,
                     cancel: threading.Event | None, bgm: Path | None = None) -> dict:
    t0 = time.time()
    if reporter:
        reporter.stage_start("render", f"合成配音（{settings.voice.split('-')[-1]}）")
    result = render.render(video, job.workdir, settings, reporter, bgm=bgm)
    if reporter:
        reporter.stage_done("render", time.time() - t0)
    _check(cancel)
    return result


def run_job(job, settings: Settings, reporter: Reporter,
            cancel: threading.Event | None = None, *, bgm: Path | None = None) -> dict:
    """跑完整流水线。已完成的阶段会跳过（按产物判断）。"""
    job.state = "running"
    job.error = None
    job.save()
    started = time.time()

    try:
        # ---- 1. 下载 / 定位源视频
        video = resolve_video(job, settings, reporter, cancel)
        _check(cancel)

        # ---- 2. 识别
        if job.stage_state("asr") == "done":
            transcript = asr.load_transcript(job.workdir)
            if reporter:
                reporter.stage_done("asr", 0.0, f"已有结果（{len(transcript.get('sentences', []))} 句）")
        else:
            transcript = run_stage_asr(job, video, settings, reporter, cancel)

        # ---- 3. 翻译
        if job.stage_state("translate") == "done":
            items = json.loads(job.file("dialogue.json").read_text(encoding="utf-8"))
            if reporter:
                reporter.stage_done("translate", 0.0, f"已有译文（{len(items)} 条）")
        else:
            items = run_stage_translate(job, transcript, settings, reporter, cancel)

        # ---- 4. 配音
        if job.stage_state("render") == "done":
            result = {
                "video": str(job.file("dubbed.mp4")),
                "audio": str(job.file("dubbed_audio.m4a"))
                if job.file("dubbed_audio.m4a").exists() else "",
                "report": _load_report(job),
                "duration": job.duration,
            }
            if reporter:
                reporter.stage_done("render", 0.0, "已有成片")
        else:
            result = run_stage_render(job, video, settings, reporter, cancel, bgm)

        # ---- 收尾
        job.state = "done"
        job.result = result
        job.refresh_stages()
        job.source["duration"] = job.source.get("duration") or result.get("duration", 0)
        job.save()
        job.clean_tmp()

        elapsed = time.time() - started
        if reporter:
            reporter.log(f"总耗时 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒")
            reporter.done({**result, "elapsed": round(elapsed, 1)})
        return result

    except Cancelled:
        job.state = "canceled"
        job.refresh_stages()
        job.save()
        if reporter:
            reporter.cancelled()
        raise
    except VideoDubError as exc:
        job.state = "failed"
        job.error = exc.to_dict()
        st = getattr(exc, "stage", "") or ""
        if st in STAGES:
            job.stages[st] = {**job.stages.get(st, {}), "state": "failed",
                              "error": exc.message}
        job.save()
        if reporter:
            reporter.error(exc.code, exc.message, exc.hint, stage=st, detail=exc.detail)
        raise
    except Exception as exc:  # noqa: BLE001
        job.state = "failed"
        job.error = {"code": "UNEXPECTED", "message": str(exc), "hint": "", "detail": ""}
        job.save()
        if reporter:
            reporter.error("UNEXPECTED", f"未预期的错误：{exc}",
                           "把这段报错和 job.json 贴到 issue")
        raise


def _load_report(job) -> dict:
    p = job.file("render_report.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def open_result(job, result: dict) -> None:
    """在文件管理器里定位成片。"""
    video = result.get("video") or str(job.file("dubbed.mp4"))
    p = Path(video)
    if p.exists():
        platforms.current().open_in_explorer(p)
    elif job.workdir.exists():
        platforms.current().open_in_explorer(job.workdir)


def check_disk(need_bytes: int, path: Path | None = None) -> None:
    import shutil

    target = path or Path.home()
    free = shutil.disk_usage(target).free
    if need_bytes > 0 and free < need_bytes * 1.2:
        raise UserInputError(
            f"磁盘空间不足：需要约 {need_bytes / 1024 ** 3:.1f} GB，"
            f"当前可用 {free / 1024 ** 3:.1f} GB",
            hint="用 videodub clean 清理旧任务，或换一个输出目录",
        )


__all__ = [
    "build_source", "build_playlist_sources", "sources_from_playlist",
    "open_result", "run_job", "resolve_video", "run_stage_asr",
    "run_stage_translate", "run_stage_render", "check_disk", "log",
]
