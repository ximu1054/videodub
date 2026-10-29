"""请求/响应模型。字段名与前端 app.js 一一对应。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Params(BaseModel):
    """界面上「高级参数」面板的 7 项，加上一个下载画质。"""

    voice: str = ""
    rate: str = ""
    max_tempo: float | None = None
    offset: float | None = None
    peak: float | None = None
    asr_model: str = ""
    asr_language: str = ""
    mt_backend: Literal["auto", "local", "api", "none"] | None = None
    max_height: int | None = None
    bgm_path: str = ""


class ProbeRequest(BaseModel):
    url: str


class PlaylistEntry(BaseModel):
    """合集里的一集（只用于界面预览，下载时服务端自己重新取全量）。"""

    index: int
    title: str = ""
    duration: float = 0.0


class ProbeResponse(BaseModel):
    ok: bool
    title: str = ""
    duration: float = 0.0
    uploader: str = ""
    thumbnail: str = ""
    error: str = ""
    # ---- 合集
    is_playlist: bool = False
    count: int = 0
    playlist_title: str = ""
    current_index: int = 0
    total_duration: float = 0.0
    max_batch: int = 0
    eta_sec: float = 0.0
    entries: list[PlaylistEntry] = []


class PreviewRequest(BaseModel):
    voice: str
    rate: str = "+8%"
    text: str = ""


class PreviewResponse(BaseModel):
    url: str


class RetryRequest(BaseModel):
    from_stage: Literal["download", "asr", "translate", "render"] = Field(
        default="translate", alias="from"
    )

    model_config = {"populate_by_name": True}


class StageInfo(BaseModel):
    state: str = "pending"
    elapsed: float = 0.0
    error: str = ""


class JobSummary(BaseModel):
    id: str
    title: str
    state: str
    state_label: str
    created: str
    duration: float = 0.0
    queue_position: int = 0
    stages: dict[str, StageInfo] = {}
    has_video: bool = False
    has_audio: bool = False
    size_mb: float = 0.0
    report: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    playable: bool = True


class ModelInfo(BaseModel):
    name: str
    cached: bool
    size_gb: float
    required: bool = True
    skip_hint: str = ""


class HealthResponse(BaseModel):
    version: str
    first_run: bool
    platform: str
    gpu: str = ""
    vram_gb: float = 0.0
    ffmpeg_ok: bool = False
    ffmpeg_detail: str = ""
    models: dict[str, ModelInfo] = {}
    queue_depth: int = 0
    queue_max: int = 10
    disk_free_gb: float = 0.0
    voices: list[dict[str, Any]] = []
    config_path: str = ""
    demucs: bool = False
    # 进程启动后又改过 .py：页面是新的、接口还是旧的，需要重启才生效
    code_stale: bool = False


class StatsResponse(BaseModel):
    total: int
    done: int
    failed: int
    running: int
    queued: int
    used_gb: float
