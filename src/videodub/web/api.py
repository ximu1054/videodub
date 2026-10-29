"""HTTP 接口。

设计取舍：

  - **进度用 SSE 不用 WebSocket**。进度是单向的；``EventSource`` 自带断线重连和
    ``Last-Event-ID``，浏览器端一行不用写；服务端用 ``StreamingResponse`` 就够。
    更重要的是省掉了 ``websockets`` 依赖（我们用 bare uvicorn）。
  - **端点用同步 def**，FastAPI 会自动丢线程池。probe / 上传 / 建任务都可能慢，
    不能挡住事件循环。
  - **所有路径都过校验**。``job_id`` 必须匹配严格格式，``name`` 必须等于
    ``Path(name).name``——本地服务同样要做路径穿越防护。
"""

from __future__ import annotations

import asyncio
import json
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .. import __version__, platforms
from ..config import config_path, load_settings, model_cached, preview_dir
from ..errors import UserInputError, VideoDubError
from ..jobs.store import Job, disk_usage, free_gb, job_dir, list_jobs, validate_id
from ..log import log
from ..media.ffmpeg import find_ffmpeg, probe
from ..media.range import range_response
from ..pipeline import (build_source, check_disk, run_job, sources_from_playlist)
from ..progress import BaseReporter
from ..stages import download as dl
from ..stages import translate
from ..stages.asr import MODEL_REPOS, MODEL_SIZES
from ..stages.tts import cache_key, render_preview
from ..util import (VIDEO_EXT, estimate_disk, estimate_seconds, gpu_info,
                    human_size)
from ..voices import PREVIEW_TEXT, VOICES
from .bus import bus
from .models import (HealthResponse, ModelInfo, Params, PreviewRequest, ProbeRequest,
                     RetryRequest)

router = APIRouter()

MAX_UPLOAD = 4 * 1024 ** 3      # 4GB
CHUNK = 1024 * 1024
BGM_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

# 一次最多提交这么多集。再多就建议分批——不是技术限制，
# 而是一百集排进去要跑十几个小时，让用户分段更容易中途调整。
PLAYLIST_MAX = 100

# 探测结果缓存：预览时解析一次，点「开始」时直接复用，省掉重复的列表页请求
_PROBE_TTL = 600.0
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}


class AppState:
    """进程内单例：一份 settings + 一个串行 worker。"""

    def __init__(self) -> None:
        from ..jobs.queue import SerialWorker

        self.settings = load_settings()
        self.worker = SerialWorker(max_queue=self.settings.max_queue)
        self.running: dict[str, threading.Event] = {}

    def submit(self, job: Job, settings) -> int:
        cancel = threading.Event()

        def _run(task) -> None:
            from ..jobs.store import Job as J

            try:
                live = J.load(task.job_id)
            except UserInputError:
                return
            reporter = WebReporter(task.job_id)
            self.running[task.job_id] = task.cancel
            bgm = None
            raw_bgm = (live.params or {}).get("bgm_path") or ""
            if raw_bgm:
                candidate = Path(raw_bgm)
                if candidate.is_file():
                    bgm = candidate
            try:
                run_job(live, settings, reporter, cancel=task.cancel, bgm=bgm)
            except VideoDubError:
                pass          # run_job 已经把状态和错误事件记好了
            finally:
                self.running.pop(task.job_id, None)

        return self.worker.submit(job.id, _run)


state = AppState()


class WebReporter(BaseReporter):
    """把事件推到总线（总线同时负责落盘供刷新回放）。"""

    def __init__(self, job_id: str, has_download: bool = True):
        super().__init__(job_id=job_id, has_download=has_download)

    def _emit(self, event) -> None:
        payload = event.to_dict()
        bus.publish(self.job_id, payload)


# ---------------------------------------------------------------- 工具
def merged_settings(params: Params):
    """在全局配置基础上套用本次任务的参数。"""
    s = load_settings()
    if params.voice:
        s.voice = params.voice
    if params.rate:
        s.rate = params.rate
    if params.max_tempo is not None:
        s.max_tempo = params.max_tempo
    if params.offset is not None:
        s.offset = params.offset
    if params.peak is not None:
        s.peak = params.peak
    if params.asr_model:
        s.asr_model = params.asr_model
    if params.asr_language:
        s.asr_language = params.asr_language
    if params.mt_backend:
        s.mt_backend = params.mt_backend
    if params.max_height:
        s.max_height = params.max_height
    s._coerce()
    return s


def _get_job(job_id: str) -> Job:
    try:
        validate_id(job_id)
        return Job.load(job_id)
    except UserInputError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


def _save_upload(upload: UploadFile, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dst.open("wb") as f:
        while True:
            chunk = upload.file.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(status_code=413,
                                    detail=f"文件超过 {human_size(MAX_UPLOAD)} 上限")
            f.write(chunk)
    return size


def _bgm_suffix(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "bgm.wav").suffix.lower()
    if suffix not in BGM_EXTS:
        raise HTTPException(status_code=400,
                            detail=f"不支持的伴奏格式 {suffix or '（无扩展名）'}，"
                                   "支持 " + "、".join(sorted(BGM_EXTS)))
    return suffix


def _attach_bgm(job: Job, upload: UploadFile | None) -> None:
    """把用户提供的伴奏存进任务目录，供 render 阶段混音。

    先把 ``bgm_path`` 清掉再判断：重试任务时旧路径可能指向已删除的文件，
    留着会让 render 拿到一个不存在的路径。
    """
    job.params.pop("bgm_path", None)
    if upload is None:
        return
    suffix = _bgm_suffix(upload)
    dst = job.workdir / f"bgm{suffix}"
    _save_upload(upload, dst)
    job.params["bgm_path"] = str(dst)
    log(f"伴奏已保存：{dst.name}（{human_size(dst.stat().st_size)}）")


def _attach_bgm_file(job: Job, src: Path, suffix: str) -> None:
    """合集批量提交时用：伴奏只上传一次，给每个任务复制一份。"""
    dst = job.workdir / f"bgm{suffix}"
    shutil.copyfile(src, dst)
    job.params["bgm_path"] = str(dst)


def _playlist_info(url: str, settings, *, refresh: bool = False) -> dict:
    """带缓存的合集探测，避免预览完再点开始又解析一遍。"""
    now = time.time()
    if not refresh:
        hit = _PROBE_CACHE.get(url)
        if hit and now - hit[0] < _PROBE_TTL:
            return hit[1]
    info = dl.probe_playlist(url, settings)
    if info.get("ok"):
        if len(_PROBE_CACHE) > 32:
            oldest = min(_PROBE_CACHE, key=lambda k: _PROBE_CACHE[k][0])
            _PROBE_CACHE.pop(oldest, None)
        _PROBE_CACHE[url] = (now, info)
    return info


def _batch_disk(entries: list[dict]) -> int:
    """合集的总磁盘估算。

    每个任务跑完都会留下源码和成片（约 2 倍源码体积），同时正在跑的那一集
    还要额外占一份中间文件。按 1080p H.264 的 4 Mbit/s 折算。
    """
    durations = [float(e.get("duration") or 0.0) for e in entries]
    total = sum(durations)
    peak = max(durations, default=0.0)
    per_sec = 0.5 * 1024 ** 2
    return int((total * 2 + peak * 2) * per_sec) + 300 * 1024 ** 2


def _create_playlist_jobs(url: str, settings, limit: int,
                          bgm: UploadFile | None) -> dict:
    """合集模式：一集一个任务，全部排进串行队列。"""
    info = _playlist_info(url, settings)
    if not info.get("ok"):
        raise HTTPException(status_code=400,
                            detail=info.get("error") or "合集解析失败")
    if not info.get("is_playlist"):
        raise HTTPException(status_code=400,
                            detail="这个链接不是合集，直接按单个视频提交就行")

    entries = list(info.get("entries") or [])
    total = int(info.get("total") or len(entries))
    if limit and int(limit) > 0:
        entries = entries[:int(limit)]
    if not entries:
        raise HTTPException(status_code=400, detail="合集里没有可下载的集数")
    if len(entries) > PLAYLIST_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多提交 {PLAYLIST_MAX} 集（这个合集有 {total} 集），"
                   f"先在「最多处理前 N 集」填 {PLAYLIST_MAX}，分批处理")

    check_disk(_batch_disk(entries))

    # 伴奏只上传一次，再复制进每个任务目录（上传流读一次就没了）
    tmp_bgm: Path | None = None
    bgm_suffix = ""
    if bgm is not None:
        bgm_suffix = _bgm_suffix(bgm)
        tmp_bgm = Path(tempfile.mkdtemp(prefix="videodub-bgm-")) / f"bgm{bgm_suffix}"
        try:
            _save_upload(bgm, tmp_bgm)
        except HTTPException:
            shutil.rmtree(tmp_bgm.parent, ignore_errors=True)
            raise

    sources = sources_from_playlist(info, url, limit=limit)
    state.worker.ensure_capacity(len(sources))

    created: list[Job] = []
    try:
        for src in sources:
            job = Job.create(source=src, params=settings.to_dict())
            if tmp_bgm is not None:
                _attach_bgm_file(job, tmp_bgm, bgm_suffix)
            job.save()
            created.append(job)
            state.submit(job, settings)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"只建好前 {len(created)} 集就停下了：{exc}",
        ) from exc
    finally:
        if tmp_bgm is not None:
            shutil.rmtree(tmp_bgm.parent, ignore_errors=True)

    log(f"合集入队 {len(created)} 集：{info.get('title', '')[:40]}")

    jobs = []
    for job in created:
        summary = job.summary()
        pos = state.worker.position(job.id)
        summary["queue_position"] = pos if pos >= 0 else 0
        summary["eta_sec"] = round(estimate_seconds(job.duration))
        jobs.append(summary)
    return {
        "ok": True, "count": len(jobs), "total": total,
        "playlist_title": info.get("title", ""),
        "jobs": jobs,
    }


# ---------------------------------------------------------------- health
@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    s = load_settings()
    gpu, vram = gpu_info()

    asr_repo = MODEL_REPOS.format(model=s.asr_model)
    asr_cached = model_cached(asr_repo)
    mt_cached = model_cached(s.mt_local_model)

    ffmpeg_ok, ffmpeg_detail = False, ""
    try:
        ffmpeg_ok = True
        ffmpeg_detail = find_ffmpeg()
    except VideoDubError as exc:
        ffmpeg_detail = exc.message

    demucs = False
    try:
        from ..stages.separate import available

        demucs = available()
    except Exception:  # noqa: BLE001
        pass

    return HealthResponse(
        version=__version__,
        first_run=not (asr_cached and mt_cached),
        platform=platforms.current().name,
        gpu=gpu, vram_gb=vram,
        ffmpeg_ok=ffmpeg_ok, ffmpeg_detail=ffmpeg_detail,
        models={
            "asr": ModelInfo(name=s.asr_model, cached=asr_cached,
                             size_gb=MODEL_SIZES.get(s.asr_model, 2.9), required=True),
            "mt": ModelInfo(name=s.mt_local_model.split("/")[-1], cached=mt_cached,
                            size_gb=5.8, required=False,
                            skip_hint="改用 API 翻译可省下 5.8GB"),
        },
        queue_depth=state.worker.depth, queue_max=s.max_queue,
        disk_free_gb=free_gb(),
        voices=VOICES,
        config_path=str(config_path()),
        demucs=demucs,
    )


@router.get("/stats")
def stats() -> dict:
    jobs = list_jobs(200)
    return {
        "total": len(jobs),
        "done": sum(1 for j in jobs if j.state == "done"),
        "failed": sum(1 for j in jobs if j.state == "failed"),
        "running": sum(1 for j in jobs if j.state == "running"),
        "queued": sum(1 for j in jobs if j.state == "queued"),
        "used_gb": disk_usage()["used_gb"],
    }


# ---------------------------------------------------------------- jobs
@router.get("/jobs")
def get_jobs(limit: int = 30) -> list[dict]:
    out = []
    for job in list_jobs(limit):
        job.refresh_stages()
        summary = job.summary()
        pos = state.worker.position(job.id)
        summary["queue_position"] = pos if pos >= 0 else 0
        out.append(summary)
    return out


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = _get_job(job_id)
    job.refresh_stages()
    summary = job.summary()
    pos = state.worker.position(job.id)
    summary["queue_position"] = pos if pos >= 0 else 0
    return summary


@router.post("/jobs")
def create_job(
    url: str = Form(""),
    params: str = Form("{}"),
    title: str = Form(""),
    scope: str = Form("single"),
    limit: int = Form(0),
    file: UploadFile | None = File(None),
    bgm: UploadFile | None = File(None),
) -> dict:
    try:
        raw_params = json.loads(params or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="参数格式错误") from exc

    p = Params(**{k: v for k, v in raw_params.items() if v not in ("", None)})
    settings = merged_settings(p)

    if not url and file is None:
        raise HTTPException(status_code=400, detail="要么给网址，要么上传文件")

    if file is not None:
        ext = Path(file.filename or "input.mp4").suffix.lower()
        if ext not in VIDEO_EXT:
            raise HTTPException(status_code=400,
                                detail=f"不支持的格式 {ext}，支持 "
                                       + "、".join(sorted(VIDEO_EXT)))

    # 翻译后端不可用就在这里拒绝，不要等识别跑完二十多秒才报错
    try:
        translate.check_ready(settings)
    except VideoDubError as exc:
        raise HTTPException(status_code=400,
                            detail=f"{exc.message}。{exc.hint or ''}".strip()) from exc

    # ---- 合集：一次建 N 个任务
    if url and scope == "playlist":
        return _create_playlist_jobs(url.strip(), settings, limit, bgm)

    # ---- 源解析
    if url:
        try:
            source, _ = build_source(url, settings)
        except VideoDubError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        if title:
            source["title"] = title
        need = int(estimate_disk(source.get("duration", 0)) * 1.2)
        check_disk(need)
        job = Job.create(source=source, params=settings.to_dict())
        try:
            _attach_bgm(job, bgm)
        except HTTPException:
            job.delete()
            raise
        job.save()
        position = state.submit(job, settings)
    else:
        assert file is not None
        suffix = Path(file.filename or "input.mp4").suffix.lower()
        if suffix not in VIDEO_EXT:
            raise HTTPException(status_code=400,
                                detail=f"不支持的格式 {suffix}，支持 "
                                       + "、".join(sorted(VIDEO_EXT)))
        job = Job.create(
            source={"kind": "file", "title": Path(file.filename or "video").stem,
                    "name": file.filename or "", "duration": 0.0, "playable": True},
            params=settings.to_dict(),
        )
        dst = job.workdir / f"source{suffix}"
        try:
            size = _save_upload(file, dst)
        except HTTPException:
            job.delete()
            raise

        info = probe(dst)
        job.source.update({
            "path": str(dst), "duration": float(info.get("duration") or 0.0),
            "size": size, "vcodec": info.get("vcodec", ""),
            "width": info.get("width", 0), "height": info.get("height", 0),
            "playable": bool(info.get("playable", True)),
        })
        try:
            _attach_bgm(job, bgm)
        except HTTPException:
            job.delete()
            raise
        job.refresh_stages()
        job.save()

        check_disk(int(estimate_disk(job.source["duration"], size) * 1.2),
                   job.workdir)
        position = state.submit(job, settings)

    log(f"新任务 {job.id}：{job.title[:50]}（排队 #{position}）")
    summary = job.summary()
    summary["queue_position"] = position
    summary["eta_sec"] = round(estimate_seconds(job.source.get("duration", 0)))
    return summary


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = _get_job(job_id)
    ok = state.worker.cancel(job_id)
    if not ok and job.state in ("queued", "running"):
        # 不在队列里但状态还是运行中（进程重启残留），直接改状态
        job.state = "canceled"
        job.save()
        ok = True
    return {"ok": ok, "state": job.state}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, req: RetryRequest) -> dict:
    job = _get_job(job_id)
    if job.state == "running" and state.worker.position(job_id) == 0:
        raise HTTPException(status_code=409, detail="任务正在运行，先取消再重试")

    stage = req.from_stage
    job.clear_from(stage)
    bus.clear(job_id)
    job.state = "queued"
    job.save()

    settings = merged_settings(Params())
    params = job.params or {}
    for key in ("voice", "rate", "max_tempo", "offset", "peak", "asr_model",
                "asr_language", "mt_backend", "max_height"):
        if key in params and params[key] not in (None, ""):
            setattr(settings, key, params[key])
    settings._coerce()

    position = state.submit(job, settings)
    return {"ok": True, "queue_position": position, "from": stage}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = _get_job(job_id)
    state.worker.cancel(job_id)
    bus.clear(job_id)
    job.delete()
    return {"ok": True}


# ---------------------------------------------------------------- 事件流
@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request,
                     last_event_id: str | None = Header(default=None)):
    job = _get_job(job_id)

    async def gen():
        after = 0
        try:
            after = int(last_event_id or 0)
        except ValueError:
            after = 0

        replayed = bus.replay(job_id, after)
        for ev in replayed:
            yield _sse(ev)
            if ev.get("kind") in ("done", "error"):
                return

        # 任务已经结束，但事件流里没拿到终态（命令行跑的任务根本没有事件日志）。
        # 不加这一段，浏览器会挂着一条永远不会有下文的连接。
        if job.state in ("done", "failed", "canceled"):
            yield _sse({
                "job_id": job_id, "seq": after + len(replayed) + 1,
                "kind": "done" if job.state == "done" else "error",
                "stage": "render", "stage_percent": 100.0, "overall_percent": 100.0,
                "message": "完成" if job.state == "done" else job.state,
                "metrics": {"state": job.state},
                "level": "info" if job.state == "done" else "error",
            })
            return

        q = bus.subscribe(job_id)
        loop = asyncio.get_running_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await loop.run_in_executor(None, _safe_get, q, 15.0)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield _sse(ev)
                if ev.get("kind") in ("done", "error"):
                    break
        finally:
            bus.unsubscribe(job_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


def _safe_get(q: queue.Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        raise


def _sse(ev: dict[str, Any]) -> str:
    kind = ev.get("kind", "progress")
    return (f"id: {ev.get('seq', 0)}\n"
            f"event: {kind}\n"
            f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")


# ---------------------------------------------------------------- 链接探测
@router.post("/probe")
def probe_link(req: ProbeRequest) -> dict:
    """读链接的标题、时长；是合集就额外告诉界面「有多少集」。"""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="网址为空")

    settings = load_settings()
    info = _playlist_info(url, settings)
    if not info.get("ok"):
        return {
            "ok": False, "title": "", "duration": 0.0, "uploader": "",
            "thumbnail": "", "error": info.get("error") or "解析失败",
            "is_playlist": False, "count": 0, "current_index": 0,
            "playlist_title": "", "total_duration": 0.0, "entries": [],
        }

    entries = info.get("entries") or []
    is_playlist = bool(info.get("is_playlist"))
    cur = next(
        (e for e in entries if e["index"] == info.get("current_index")), None
    )

    if is_playlist and cur:
        title = cur["title"]
        duration = float(cur["duration"] or 0.0)
    else:
        title = info.get("title") or ""
        duration = float(info.get("duration") or 0.0)

    total_duration = (sum(float(e["duration"] or 0.0) for e in entries)
                      if is_playlist else duration)
    count = int(info.get("count") or len(entries))

    out = {
        "ok": True,
        "title": title,
        "duration": duration,
        "uploader": info.get("uploader", ""),
        "thumbnail": info.get("thumbnail", ""),
        "error": "",
        "is_playlist": is_playlist,
        "count": count,
        "playlist_title": info.get("title", "") if is_playlist else "",
        "current_index": int(info.get("current_index") or 0) if is_playlist else 0,
        "total_duration": round(total_duration, 1),
        "max_batch": PLAYLIST_MAX,
        # 前 20 集给界面做个「确实读到了」的佐证，不用整份传
        "entries": [
            {"index": e["index"], "title": e["title"],
             "duration": round(float(e["duration"] or 0.0), 1)}
            for e in entries[:20]
        ] if is_playlist else [],
    }
    if total_duration:
        out["eta_sec"] = round(estimate_seconds(total_duration))
    return out


# ---------------------------------------------------------------- 音色
@router.post("/voices/preview")
def voices_preview(req: PreviewRequest) -> dict:
    text = req.text.strip() or PREVIEW_TEXT
    dst = preview_dir() / f"{cache_key(req.voice, req.rate, text)}.mp3"
    try:
        out = render_preview(text, req.voice, req.rate, dst)
    except VideoDubError as exc:
        raise HTTPException(status_code=503,
                            detail=exc.hint or exc.message) from exc
    return {"url": f"/api/preview/{out.name}"}


@router.get("/preview/{name}")
def get_preview(name: str, request: Request):
    if Path(name).name != name:
        raise HTTPException(status_code=400, detail="非法文件名")
    return range_response(preview_dir() / name, request)


# ---------------------------------------------------------------- 媒体
@router.get("/media/{job_id}/{name}")
def get_media(job_id: str, name: str, request: Request):
    safe = Path(name).name
    if safe != name:
        raise HTTPException(status_code=400, detail="非法文件名")
    try:
        target = job_dir(job_id) / safe
    except UserInputError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return range_response(target, request)


@router.get("/jobs/{job_id}/text")
def get_text(job_id: str, kind: str = "dialogue") -> JSONResponse:
    """译文预览（界面上「查看译文」用）。"""
    job = _get_job(job_id)
    name = {"dialogue": "dialogue.json", "transcript": "transcript.json"}.get(
        kind, "dialogue.json")
    p = job.file(name)
    if not p.exists():
        raise HTTPException(status_code=404, detail="还没有这个文件")
    try:
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="文件损坏") from exc


@router.post("/open/{job_id}")
def open_folder(job_id: str) -> dict:
    job = _get_job(job_id)
    target = job.file("dubbed.mp4")
    platforms.current().open_in_explorer(target if target.exists() else job.workdir)
    return {"ok": True}


__all__ = ["router", "state", "WebReporter"]
