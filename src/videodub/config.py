"""配置：所有默认值的**唯一来源**。

旧版三个入口（dub_auto / render / batch）各有一份默认值且互不相同，
是「同一条命令跑出不同结果」的主要来源。现在统一到 ``DEFAULTS``。

配置文件位置（优先级从高到低）：
  1. ``$VIDEODUB_CONFIG`` 指定的路径
  2. ``$VIDEODUB_HOME/config.toml``
  3. 平台默认：
       Windows  %LOCALAPPDATA%\\VideoDub\\config.toml
       macOS    ~/Library/Application Support/VideoDub/config.toml
       Linux    ~/.local/share/videodub/config.toml
"""

from __future__ import annotations

import os
import socket
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 默认值
DEFAULTS: dict[str, Any] = {
    # 识别
    "asr_model": "large-v3",
    "asr_device": "auto",          # auto | cuda | cpu
    "asr_language": "",            # 空 = 自动识别
    # 翻译
    "mt_backend": "auto",          # auto | local | api | none
    "mt_local_model": "Qwen/Qwen2.5-3B-Instruct",
    "mt_batch": 8,
    "mt_retry": 2,
    # 配音
    "voice": "zh-CN-XiaoxiaoNeural",
    "rate": "+8%",
    "max_tempo": 1.15,
    "peak": 0.85,
    "offset": 0.0,
    "bgm_gain": 1.0,
    "tts_concurrency": 4,
    # 下载
    "max_height": 1080,
    # 网络
    "hf_mirror": "auto",           # auto | none | hf-mirror | <url>
    "proxy": "auto",               # auto | none | <url>
    # 服务
    "host": "127.0.0.1",
    "port": 8765,
    "open_browser": True,
    "max_queue": 10,
}

VOICE_DEFAULT = DEFAULTS["voice"]


# ---------------------------------------------------------------- 目录
def app_home() -> Path:
    """配置与任务数据的根目录。可用 ``VIDEODUB_HOME`` 覆盖（便携部署）。"""
    env = os.environ.get("VIDEODUB_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VideoDub"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VideoDub"
    return Path.home() / ".local" / "share" / "videodub"


def data_dir() -> Path:
    """任务是数据：每个任务一个目录。"""
    return app_home() / "jobs"


def preview_dir() -> Path:
    """音色试听的临时 mp3。"""
    return app_home() / "preview"


def config_path() -> Path:
    env = os.environ.get("VIDEODUB_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return app_home() / "config.toml"


# ---------------------------------------------------------------- 配置对象
@dataclass
class Settings:
    asr_model: str = DEFAULTS["asr_model"]
    asr_device: str = DEFAULTS["asr_device"]
    asr_language: str = DEFAULTS["asr_language"]

    mt_backend: str = DEFAULTS["mt_backend"]
    mt_local_model: str = DEFAULTS["mt_local_model"]
    mt_batch: int = DEFAULTS["mt_batch"]
    mt_retry: int = DEFAULTS["mt_retry"]
    mt_api_base: str = ""
    mt_api_model: str = ""
    mt_api_key: str = ""           # 也可用环境变量 VIDEODUB_API_KEY

    voice: str = DEFAULTS["voice"]
    rate: str = DEFAULTS["rate"]
    max_tempo: float = DEFAULTS["max_tempo"]
    peak: float = DEFAULTS["peak"]
    offset: float = DEFAULTS["offset"]
    bgm_gain: float = DEFAULTS["bgm_gain"]
    tts_concurrency: int = DEFAULTS["tts_concurrency"]

    max_height: int = DEFAULTS["max_height"]

    hf_mirror: str = DEFAULTS["hf_mirror"]
    proxy: str = DEFAULTS["proxy"]

    host: str = DEFAULTS["host"]
    port: int = DEFAULTS["port"]
    open_browser: bool = DEFAULTS["open_browser"]
    max_queue: int = DEFAULTS["max_queue"]

    # 非持久化：运行时解析出的实际值
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    # -------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Settings:
        known = {f.name for f in fields(cls)} - {"extra"}
        clean: dict[str, Any] = {}
        for k, v in raw.items():
            if k not in known or v is None:
                continue
            clean[k] = v
        obj = cls(**clean)
        obj._coerce()
        return obj

    def _coerce(self) -> None:
        """TOML 里数字可能写成整数，统一成期望类型。"""
        self.max_tempo = float(self.max_tempo)
        self.peak = float(self.peak)
        self.offset = float(self.offset)
        self.bgm_gain = float(self.bgm_gain)
        self.port = int(self.port)
        self.max_height = int(self.max_height)
        self.mt_batch = int(self.mt_batch)
        self.mt_retry = int(self.mt_retry)
        self.tts_concurrency = int(self.tts_concurrency)
        self.max_queue = int(self.max_queue)
        # 越界兜底：错的值比崩掉更糟
        self.max_tempo = min(max(self.max_tempo, 1.0), 2.0)
        self.peak = min(max(self.peak, 0.1), 1.0)
        self.tts_concurrency = min(max(self.tts_concurrency, 1), 8)
        if self.asr_device not in ("auto", "cuda", "cpu"):
            self.asr_device = "auto"
        if self.mt_backend not in ("auto", "local", "api", "none"):
            self.mt_backend = "auto"

    # -------------------------------------------------- 语速字符串
    @property
    def rate_percent(self) -> int:
        """把 "+8%" 解析成 8，方便界面滑杆使用。"""
        raw = (self.rate or "+0%").strip().replace("%", "")
        try:
            return int(raw)
        except ValueError:
            return 0

    @rate_percent.setter
    def rate_percent(self, value: int) -> None:
        self.rate = f"{int(value):+d}%"


# ---------------------------------------------------------------- 读写
def load_settings(path: Path | None = None) -> Settings:
    p = path or config_path()
    if not p.exists():
        return Settings()
    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return Settings()

    flat: dict[str, Any] = {}
    for section, body in raw.items():
        if not isinstance(body, dict):
            continue
        prefix = section.replace(".", "_")
        for key, value in body.items():
            if isinstance(value, dict):
                # 二级段落，例如 [mt.api] —— save_settings 就是这么写的
                for sub, sub_value in value.items():
                    flat[f"{prefix}_{key}_{sub}"] = sub_value
            else:
                flat[f"{prefix}_{key}"] = value
    # 兼容扁平写法
    for key, value in raw.items():
        if not isinstance(value, dict):
            flat[key] = value

    # 段落名到字段名的重映射
    alias = {
        "asr_model": "asr_model", "asr_device": "asr_device", "asr_language": "asr_language",
        "mt_backend": "mt_backend", "mt_local_model": "mt_local_model",
        "mt_batch": "mt_batch", "mt_retry": "mt_retry",
        "mt_api_base": "mt_api_base", "mt_api_model": "mt_api_model", "mt_api_key": "mt_api_key",
        # [mt.api] 段落里写的是 base_url / model，拼出来是 mt_api_base_url
        "mt_api_base_url": "mt_api_base", "mt_api_model_name": "mt_api_model",
        "tts_voice": "voice", "tts_rate": "rate", "tts_concurrency": "tts_concurrency",
        "render_max_tempo": "max_tempo", "render_peak": "peak",
        "render_offset": "offset", "render_bgm_gain": "bgm_gain",
        "network_hf_mirror": "hf_mirror", "network_proxy": "proxy",
        "server_host": "host", "server_port": "port",
        "server_open_browser": "open_browser", "server_max_queue": "max_queue",
    }
    resolved: dict[str, Any] = {}
    for key, value in flat.items():
        resolved[alias.get(key, key)] = value

    # API key 优先级：环境变量 > 配置文件
    env_key = os.environ.get("VIDEODUB_API_KEY") or os.environ.get("TRANSLATE_API_KEY")
    if env_key:
        resolved["mt_api_key"] = env_key
    if not resolved.get("mt_api_base"):
        resolved["mt_api_base"] = os.environ.get("VIDEODUB_API_BASE", "")

    return Settings.from_dict(resolved)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """把配置按段落写回 TOML。不写 API key（建议走环境变量）。"""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    s = settings
    lines = [
        "# VideoDub 配置。全部项都有默认值，这里只需写你要改的。",
        "",
        "[asr]",
        f'model = "{s.asr_model}"',
        f'device = "{s.asr_device}"',
        f'language = "{s.asr_language}"',
        "",
        "[mt]",
        f'backend = "{s.mt_backend}"',
        f'local_model = "{s.mt_local_model}"',
        f"batch = {s.mt_batch}",
        f"retry = {s.mt_retry}",
        "",
        "[mt.api]",
        f'base_url = "{s.mt_api_base}"',
        f'model = "{s.mt_api_model}"',
        "# api_key 建议用环境变量 VIDEODUB_API_KEY，别写进文件",
        "",
        "[tts]",
        f'voice = "{s.voice}"',
        f'rate = "{s.rate}"',
        f"concurrency = {s.tts_concurrency}",
        "",
        "[render]",
        f"max_tempo = {s.max_tempo}",
        f"peak = {s.peak}",
        f"offset = {s.offset}",
        f"bgm_gain = {s.bgm_gain}",
        "",
        "[network]",
        f'hf_mirror = "{s.hf_mirror}"',
        f'proxy = "{s.proxy}"',
        "",
        "[server]",
        f'host = "{s.host}"',
        f"port = {s.port}",
        f"open_browser = {str(s.open_browser).lower()}",
        f"max_queue = {s.max_queue}",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------- HuggingFace 环境
_MIRROR = "https://hf-mirror.com"
_probe_cache: dict[str, bool] = {}


def _reachable(host: str, port: int = 443, timeout: float = 2.0) -> bool:
    if host in _probe_cache:
        return _probe_cache[host]
    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    _probe_cache[host] = ok
    return ok


def resolve_hf_endpoint(mirror: str = "auto") -> str | None:
    """把 hf_mirror 配置解析成实际的 endpoint。

    ``auto`` 的行为很重要：先用 2 秒探测 huggingface.co，通就直连。
    旧版硬编码 hf-mirror.com，对中国用户友好，但对海外用户是灾难——
    而 GitHub 的主要受众在海外。
    """
    mode = (mirror or "auto").strip()
    if mode == "none":
        return None
    if mode == "hf-mirror":
        return _MIRROR
    if mode.startswith("http"):
        return mode.rstrip("/")
    # auto
    if _reachable("huggingface.co"):
        return None
    return _MIRROR


def hf_env_for_model(repo_id: str, mirror: str = "auto") -> dict[str, str]:
    """给 huggingface_hub 准备环境变量，并直接写进 os.environ。

    模型已缓存就切离线（避免每次启动都去网上校验），没缓存才设 endpoint。
    返回值方便调用方记录/调试。
    """
    env: dict[str, str] = {"HF_HUB_DISABLE_XET": "1"}
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    if model_cached(repo_id):
        env["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"
        # 离线时不能残留 endpoint，否则部分版本仍会尝试联网
        os.environ.pop("HF_ENDPOINT", None)
        return env

    os.environ.pop("HF_HUB_OFFLINE", None)
    endpoint = resolve_hf_endpoint(mirror)
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
        os.environ["HF_ENDPOINT"] = endpoint
    else:
        os.environ.pop("HF_ENDPOINT", None)
    return env


def model_cached(repo_id: str) -> bool:
    home = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    return (home / "hub" / ("models--" + repo_id.replace("/", "--"))).exists()
