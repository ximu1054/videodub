"""翻译：把英文讲稿译成中文。

两个后端：
  - ``local``  Qwen2.5-3B-Instruct，本机 GPU，离线、免费、不限量
  - ``api``    任意 OpenAI 兼容接口（DeepSeek / 通义 / 本地 vLLM 都行），更快更准
  - ``auto``   配了 key 走 api，否则走 local

两个机制保留自旧版（都是好东西）：
  - **逐批落盘**：``translated.json`` 是 ``{"单元序号": "译文"}``，
    中断后用 ``--continue`` 直接续跑，不重复花钱也不重复等。
  - **缺译退回原文**：宁可念一句英文，也不要留一段静音——
    静音会被用户误认为程序卡住了。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..config import Settings, hf_env_for_model, model_cached
from ..errors import AuthError, MissingDependencyError, NetworkError, VideoDubError
from ..log import log
from ..progress import Reporter
from .prompts import build_system_prompt, build_user_prompt, retranslate_prompt
from .units import audit_budget

CJK = re.compile(r"[\u4e00-\u9fff]")
STAGE = "translate"


# ---------------------------------------------------------------- 文本后处理
def clean_line(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^\s*[-*•]\s*", "", s)
    s = re.sub(r"^[\"'“”「」]|[\"'“”「」]$", "", s)
    return s.strip()


def parse_numbered(raw: str, n_expect: int) -> dict[int, str]:
    """从模型输出里解析「序号. 译文」。

    容忍全角点、中文顿号、括号、冒号——模型这些花样都会出。
    """
    out: dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,3})\s*[.、．:：)）]\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        txt = clean_line(m.group(2))
        if 0 <= idx < n_expect and txt:
            out[idx] = txt
    return out


def is_chinese(text: str, ratio: float = 0.25) -> bool:
    if not text:
        return False
    return len(CJK.findall(text)) / max(len(text), 1) >= ratio


# ---------------------------------------------------------------- 本地后端
class LocalTranslator:
    where = "local"

    def __init__(self, model_name: str = "Qwen/Qwen2.5-3B-Instruct",
                 device: str = "auto", mirror: str = "auto"):
        hf_env_for_model(model_name, mirror)
        try:
            import torch
            import transformers
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise MissingDependencyError(
                "本地翻译需要 torch 与 transformers",
                hint='安装：pip install -e ".[local-mt]"（有显卡请先装 CUDA 版 torch，见 README）',
                detail=str(exc),
            ) from exc

        self.torch = torch
        self.model_name = model_name
        # 模型已在本地就绝不碰网络：HF_HUB_OFFLINE 是 import 时读的常量，
        # 这里再设已经晚了，只有 local_files_only 能真正拦住联网。
        local_only = model_cached(model_name)
        try:
            self.tok = AutoTokenizer.from_pretrained(model_name, local_files_only=local_only)
        except Exception as exc:  # noqa: BLE001
            raise self._load_error(exc) from exc

        kw: dict = {"low_cpu_mem_usage": True}
        # transformers 5.x 把 torch_dtype 改名成 dtype，旧名会打弃用告警
        major = int(str(transformers.__version__).split(".")[0] or 0)
        if major >= 5:
            kw["dtype"] = torch.float16
        else:
            kw["torch_dtype"] = torch.float16

        use_cuda = device in ("auto", "cuda") and torch.cuda.is_available()
        kw["device_map"] = "cuda" if use_cuda else "cpu"
        if not use_cuda:
            # CPU 上 float16 极慢且部分算子不支持
            kw.pop("dtype", None)
            kw.pop("torch_dtype", None)

        kw["local_files_only"] = local_only
        try:
            self.model = AutoModelForCausalLM.from_pretrained(model_name, **kw)
        except Exception as exc:  # noqa: BLE001
            raise self._load_error(exc) from exc
        self.model.eval()
        self.where = f"本地 {model_name.split('/')[-1]}（{'GPU' if use_cuda else 'CPU'}）"

    @staticmethod
    def _load_error(exc: Exception) -> Exception:
        """把「连不上仓库」翻译成看得懂的提示，别让用户对着 httpx 堆栈发呆。"""
        name = type(exc).__name__.lower()
        module = type(exc).__module__.split(".", 1)[0]
        if any(k in name for k in ("proxy", "connect", "timeout", "sslerror", "httperror")) or \
                module in {"httpx2", "httpx", "httpcore", "httpcore2", "requests", "urllib3"}:
            return NetworkError(
                "加载本地翻译模型时连不上 HuggingFace",
                hint=("模型还没下载完整。检查代理设置，或把 [network] hf_mirror 设成 "
                      "hf-mirror.com 走国内镜像；也可以 pip install -e \".[local-mt]\" "
                      "后用 videodub models --mt 预下载。"),
                detail=f"{type(exc).__name__}: {exc}"[:800],
            )
        return exc

    def _generate(self, system: str, user: str, max_new: int) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        prompt = self.tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = self.tok(prompt, return_tensors="pt").to(self.model.device)
        with self.torch.no_grad():
            ids = self.model.generate(
                **inputs, max_new_tokens=max_new, do_sample=False,
                repetition_penalty=1.05, pad_token_id=self.tok.eos_token_id,
            )
        gen = ids[0][inputs["input_ids"].shape[1]:]
        return self.tok.decode(gen, skip_special_tokens=True)

    def translate_batch(self, items: list[str], context: str = "") -> list[str]:
        system = build_system_prompt()
        user = build_user_prompt(items, context)
        raw = self._generate(system, user, min(2048, 90 * len(items) + 120))
        got = parse_numbered(raw, len(items))
        return [got.get(i, "") for i in range(len(items))]

    def translate_one_short(self, text: str, limit_chars: int) -> str:
        raw = self._generate(build_system_prompt(), retranslate_prompt(text, limit_chars), 256)
        lines = [clean_line(x) for x in raw.splitlines() if clean_line(x)]
        if not lines:
            return ""
        best = min(lines, key=len)
        return best if is_chinese(best) else ""


# ---------------------------------------------------------------- API 后端
class ApiTranslator:
    """OpenAI 兼容接口。配置优先级：环境变量 > config.toml。"""

    where = "api"

    def __init__(self, base_url: str = "", api_key: str = "", model: str = ""):
        self.base = (base_url or os.environ.get("VIDEODUB_API_BASE")
                     or os.environ.get("TRANSLATE_BASE_URL")
                     or "https://api.deepseek.com/v1").rstrip("/")
        self.key = (api_key or os.environ.get("VIDEODUB_API_KEY")
                    or os.environ.get("TRANSLATE_API_KEY") or "")
        self.model = (model or os.environ.get("TRANSLATE_MODEL") or "deepseek-chat")
        if not self.key:
            raise AuthError(
                "没有配置翻译 API key",
                hint="设置环境变量 VIDEODUB_API_KEY，或在 config.toml 的 [mt.api] 里填 api_key；"
                     "也可以用 backend=local 走本地模型",
            )
        self.where = f"API {self.model}"

    def _post(self, system: str, user: str, timeout: int = 180) -> str:
        import urllib.error
        import urllib.request

        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
        }).encode()
        req = urllib.request.Request(
            self.base + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                data = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            hint = "检查 API key 是否有效、余额是否充足"
            if exc.code in (401, 403):
                raise AuthError(f"API 返回 {exc.code}（认证失败）", hint=hint) from exc
            if exc.code == 429:
                raise NetworkError("API 限流（429）", hint="降低批次大小或稍后重试") from exc
            raise NetworkError(f"API 返回 {exc.code}", hint=hint) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # OSError 覆盖 ConnectionResetError / 连接被中途掐断这类情况——
            # 它不会被打包成 URLError，会直接往上抛，不接住就是一段 traceback
            raise NetworkError("连不上翻译 API", hint="检查网络 / 代理设置",
                               detail=f"{type(exc).__name__}: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise NetworkError("翻译 API 返回了意外的结构", detail=str(data)[:500]) from exc

    def translate_batch(self, items: list[str], context: str = "") -> list[str]:
        raw = self._post(build_system_prompt(), build_user_prompt(items, context))
        got = parse_numbered(raw, len(items))
        return [got.get(i, "") for i in range(len(items))]

    def translate_one_short(self, text: str, limit_chars: int) -> str:
        raw = self._post(build_system_prompt(), retranslate_prompt(text, limit_chars), 120)
        lines = [clean_line(x) for x in raw.splitlines() if clean_line(x)]
        return min(lines, key=len) if lines else ""


def make_translator(settings: Settings):
    """按配置挑后端。``auto`` 的语义：能走 API 就走 API（快很多）。"""
    backend = settings.mt_backend
    if backend == "api":
        return ApiTranslator(settings.mt_api_base, settings.mt_api_key, settings.mt_api_model)
    if backend == "local":
        return LocalTranslator(settings.mt_local_model, settings.asr_device, settings.hf_mirror)
    try:
        return ApiTranslator(settings.mt_api_base, settings.mt_api_key, settings.mt_api_model)
    except VideoDubError:
        return LocalTranslator(settings.mt_local_model, settings.asr_device, settings.hf_mirror)


def check_ready(settings: Settings) -> str:
    """**不加载模型**地确认翻译后端能用，返回将要用的后端名。

    为什么要有这个东西：显式配了 ``backend=api`` 却没配 key 时，
    过去要等识别跑完二十多秒才在翻译阶段报错——用户白等一场。
    提交任务前先问一句，失败得越早越好。

    只做静态检查（有没有 key / 有没有装包），不发请求、不读权重。
    """
    backend = (settings.mt_backend or "auto").strip()
    if backend == "none":
        return "none"

    has_key = bool(settings.mt_api_key or os.environ.get("VIDEODUB_API_KEY")
                   or os.environ.get("TRANSLATE_API_KEY"))

    if backend == "api" or (backend == "auto" and has_key):
        if not has_key:
            raise AuthError(
                "配置里指定了用 API 翻译，但没有 API key",
                hint="设置环境变量 VIDEODUB_API_KEY（推荐），或在 config.toml 的 "
                     "[mt.api] 里填 api_key；也可以把 backend 改成 local 或 auto",
            )
        return "api"

    # local（显式）或 auto 且没有 key
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as exc:
        if backend == "local":
            raise MissingDependencyError(
                "配置里指定了本地翻译，但没装 torch / transformers",
                hint='pip install -e ".[local-mt]"；或者配一个 API key 走 API 翻译',
                detail=str(exc),
            ) from exc
        raise MissingDependencyError(
            "没有可用的翻译后端",
            hint="两条路选一条：设置 VIDEODUB_API_KEY 走 API（快），"
                 '或 pip install -e ".[local-mt]" 用本地模型（免费、离线）',
            detail=str(exc),
        ) from exc
    return "local"


# ---------------------------------------------------------------- 主流程
def _cache_path(workdir: Path) -> Path:
    return workdir / "translated.json"


def load_cache(workdir: Path) -> dict[str, str]:
    p = _cache_path(workdir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, json.JSONDecodeError):
        return {}


def translate_units(
    units: list[dict],
    *,
    workdir: Path,
    settings: Settings,
    context: str = "",
    reporter: Reporter | None = None,
    force: bool = False,
) -> dict[str, str]:
    """翻译所有单元，返回 ``{"序号": "译文"}``。

    会自动读取/写入 ``translated.json``，所以中断后重跑是免费的。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    done = {} if force else load_cache(workdir)
    if done and reporter:
        reporter.log(f"已有 {len(done)} 条译文缓存，续跑")

    todo = [(i, u) for i, u in enumerate(units) if str(i) not in done]
    total = len(units)

    if settings.mt_backend == "none":
        if reporter:
            reporter.log("已选择「只转写不翻译」，跳过翻译")
        return done

    if todo:
        translator = make_translator(settings)
        if reporter:
            reporter.log(f"翻译后端：{translator.where}，共 {len(todo)} 条待译")
        batch = max(int(settings.mt_batch), 1)
        cache_path = _cache_path(workdir)
        t0 = time.time()
        failed_batches = 0

        for b in range(0, len(todo), batch):
            chunk = todo[b:b + batch]
            texts = [u["text"] for _, u in chunk]
            got: list[str] = []
            for attempt in range(settings.mt_retry + 1):
                try:
                    got = translator.translate_batch(texts, context)
                    if all(got):
                        break
                except VideoDubError as exc:
                    failed_batches += 1
                    if reporter:
                        reporter.log(f"第 {b // batch + 1} 批失败：{exc.message}", "warn")
                    if exc.code in ("AUTH", "MISSING_DEP"):
                        raise
                    time.sleep(2 * (attempt + 1))

            for k, (uidx, _) in enumerate(chunk):
                if k < len(got) and got[k]:
                    done[str(uidx)] = got[k]
            cache_path.write_text(
                json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8"
            )

            pct = min(100.0, (b + len(chunk)) / max(len(todo), 1) * 100.0)
            elapsed = time.time() - t0
            eta = (elapsed / max(pct, 1)) * (100 - pct)
            if reporter:
                reporter.progress(
                    STAGE, pct,
                    message=f"第 {b // batch + 1}/{(len(todo) + batch - 1) // batch} 批"
                            f"，已译 {len(done)}/{total}",
                    metrics={"done": len(done), "total": total,
                             "eta_sec": round(eta, 0), "failed_batches": failed_batches},
                )
        if reporter:
            reporter.log(f"翻译耗时 {time.time() - t0:.0f} 秒")
    elif reporter:
        reporter.progress(STAGE, 100, message=f"全部命中缓存（{total} 条）")

    # ---- 长度预算：装不下的单条重译（让模型写短，比事后压语速好听）
    over = audit_budget(units, done)
    if over:
        if reporter:
            reporter.log(f"{len(over)} 条译文预计装不进时间槽，尝试压缩重译")
        try:
            translator = make_translator(settings)
        except VideoDubError:
            translator = None
        if translator is not None:
            fixed = 0
            for n, (idx, verdict) in enumerate(over):
                try:
                    shorter = translator.translate_one_short(
                        units[idx]["text"], verdict.limit_chars
                    )
                except VideoDubError:
                    break
                if shorter and len(shorter) < len(done.get(str(idx), "")):
                    done[str(idx)] = shorter
                    fixed += 1
                if reporter and n % 5 == 0:
                    reporter.progress(
                        STAGE, 99.0,
                        message=f"压缩重译 {n + 1}/{len(over)}",
                        metrics={"fixed": fixed, "total": len(over)},
                    )
            _cache_path(workdir).write_text(
                json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            if reporter:
                reporter.log(f"压缩生效 {fixed}/{len(over)} 条")

    missing = sum(1 for i in range(total) if not done.get(str(i)))
    if missing and reporter:
        reporter.log(f"{missing} 条未译出，将退回原文（不会静音）", "warn")
    return done


def zh_ratio(units: list[dict], done: dict[str, str]) -> float:
    if not units:
        return 0.0
    n = sum(1 for i in range(len(units)) if is_chinese(done.get(str(i), "")))
    return n / len(units)


def write_dialogue(workdir: Path, items: list[dict]) -> Path:
    p = workdir / "dialogue.json"
    p.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def skip_reason(language: str) -> str:
    """原视频本来就是中文时不用翻。"""
    lang = (language or "").lower()
    return "原音轨已是中文，跳过翻译" if lang.startswith("zh") else ""


__all__ = [
    "ApiTranslator", "LocalTranslator", "STAGE", "clean_line", "is_chinese",
    "load_cache", "make_translator", "parse_numbered", "translate_units",
    "write_dialogue", "zh_ratio", "log",
]
