"""命令行入口。

退出码有语义，方便脚本判断失败类型（见 errors.py）：
  0 成功 / 1 未分类 / 2 输入有误 / 3 缺依赖 / 4 网络 / 5 媒体处理 / 130 中断
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULTS, Settings, config_path, load_settings, save_settings
from .errors import UserInputError, VideoDubError
from .log import banner, log, set_quiet
from .progress import StdoutReporter
from .util import hr, human_size


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="videodub",
        description="把英文视频整条换成中文配音，逐句对齐原时间轴。",
        epilog="不带子命令直接运行 = 启动网页界面。",
    )
    ap.add_argument("-V", "--version", action="version", version=f"VideoDub {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    # ---- run
    p = sub.add_parser("run", help="转一个视频（网址或本地文件）")
    p.add_argument("target", help="视频网址，或本地视频路径")
    p.add_argument("--voice", default=None, help=f"音色 ID，默认 {DEFAULTS['voice']}")
    p.add_argument("--rate", default=None, help="语速，如 +8%% / +0%% / -4%%")
    p.add_argument("--max-tempo", type=float, default=None, help="单句最多压缩到多少倍速")
    p.add_argument("--offset", type=float, default=None, help="整条音轨平移（秒），负值=提前")
    p.add_argument("--peak", type=float, default=None, help="人声峰值")
    p.add_argument("--asr-model", default=None, help="tiny/base/small/medium/large-v3")
    p.add_argument("--device", default=None, choices=["auto", "cuda", "cpu"])
    p.add_argument("--mt-backend", default=None, choices=["auto", "local", "api", "none"])
    p.add_argument("--bgm", default=None, help="伴奏 wav（会做侧链压缩，说话时自动压低）")
    p.add_argument("--height", type=int, default=None, help="下载画质上限，默认 1080")
    p.add_argument("--force", default=None, choices=["download", "asr", "translate", "render"],
                   help="从某个阶段重跑（会清掉该阶段及下游产物）")
    p.add_argument("--open", action="store_true", help="完成后打开所在文件夹")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果（给脚本用）")
    p.add_argument("-q", "--quiet", action="store_true")

    # ---- serve
    p = sub.add_parser("serve", help="启动网页界面")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    # ---- doctor
    sub.add_parser("doctor", help="检查运行环境，输出可贴进 issue 的报告")

    # ---- voices
    p = sub.add_parser("voices", help="列出可用中文音色")
    p.add_argument("--preview", metavar="VOICE", help="试听某个音色（需联网）")

    # ---- models
    p = sub.add_parser("models", help="查看/预下载模型")
    p.add_argument("--download", action="store_true", help="下载识别模型")
    p.add_argument("--mt", action="store_true", help="连翻译模型一起下载（约 5.8GB）")

    # ---- clean
    p = sub.add_parser("clean", help="清理旧任务")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--all", action="store_true", help="连成品一起删")

    # ---- config
    p = sub.add_parser("config", help="查看或初始化配置文件")
    p.add_argument("--init", action="store_true", help="写入默认配置文件")
    p.add_argument("--path", action="store_true", help="只打印配置文件路径")

    return ap


# ---------------------------------------------------------------- run
def _merge_settings(args) -> Settings:
    s = load_settings()
    for attr, key in (
        ("voice", "voice"), ("rate", "rate"), ("max_tempo", "max_tempo"),
        ("offset", "offset"), ("peak", "peak"), ("asr_model", "asr_model"),
        ("device", "asr_device"), ("mt_backend", "mt_backend"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            setattr(s, key, val)
    if getattr(args, "height", None) is not None:
        s.max_height = args.height
    s._coerce()
    return s


def cmd_run(args) -> int:
    from .jobs.store import Job
    from .pipeline import build_source, open_result, run_job
    from .stages.translate import check_ready
    from . import pipeline

    settings = _merge_settings(args)
    set_quiet(args.quiet)

    # 先确认翻译后端可用：别让用户等识别跑完才发现没配 API key
    try:
        backend = check_ready(settings)
    except VideoDubError as exc:
        log(f"✗ {exc.message}", "error")
        if exc.hint:
            log(f"  提示：{exc.hint}", "error")
        return exc.exit_code
    if backend != "none" and not args.quiet:
        log(f"翻译后端：{backend}")

    source, _ = build_source(args.target, settings)
    job = Job.create(source=source, params=settings.to_dict())

    if args.force:
        job.clear_from(args.force)

    if not args.quiet:
        banner(f"VideoDub · {source['title'][:60]}")
        if source.get("duration"):
            log(f"时长 {hr(source['duration'])}")

    reporter = StdoutReporter(job_id=job.id, has_download=source.get("kind") == "url",
                             verbose=not args.quiet)

    bgm = Path(args.bgm).expanduser().resolve() if args.bgm else None
    try:
        result = run_job(job, settings, reporter, bgm=bgm)
    except VideoDubError:
        return 1

    if args.json:
        print(json.dumps({
            "job_id": job.id, "state": job.state,
            "video": result.get("video"), "audio": result.get("audio"),
            "report": result.get("report", {}),
        }, ensure_ascii=False, indent=1))
    elif not args.quiet:
        banner("完成")
        log(f"成片：{result.get('video')}")
        if audio := result.get("audio"):
            log(f"音轨：{audio}")
        report = result.get("report") or {}
        if report:
            log(f"铺入 {report.get('placed')} 句 · "
                f"压缩 {report.get('tempo_compressed')} 句 · "
                f"最大漂移 {report.get('drift_max')} 秒")

    if args.open:
        open_result(job, result)
    return 0


# ---------------------------------------------------------------- serve
def cmd_serve(args) -> int:
    from .web.app import serve

    settings = load_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    return serve(settings, open_browser=not args.no_browser)


# ---------------------------------------------------------------- doctor
def cmd_doctor(_args) -> int:
    from .doctor import run_checks, format_report

    results = run_checks()
    print(format_report(results))
    return 0 if all(r.ok for r in results if r.required) else 1


# ---------------------------------------------------------------- voices
def cmd_voices(args) -> int:
    from .voices import PREVIEW_TEXT, VOICES

    if args.preview:
        from .config import preview_dir
        from .stages.tts import render_preview
        from .stages.tts import cache_key

        settings = load_settings()
        dst = preview_dir() / f"{cache_key(args.preview, settings.rate, PREVIEW_TEXT)}.mp3"
        try:
            out = render_preview(PREVIEW_TEXT, args.preview, settings.rate, dst)
        except VideoDubError as exc:
            log(f"试听失败：{exc.message}", "error")
            if exc.hint:
                log(f"提示：{exc.hint}", "warn")
            return exc.exit_code
        print(str(out))
        return 0

    print(f"{'ID':<38} {'名称':<6} {'性别':<4} 特点")
    print("-" * 66)
    for v in VOICES:
        mark = " ★" if v.get("recommend") else ("  (实验)" if v.get("experimental") else "")
        print(f"{v['id']:<38} {v['name']:<6} {v['gender']:<4} {'/'.join(v['tags'])}{mark}")
    print("\n试听：videodub voices --preview zh-CN-XiaoxiaoNeural")
    return 0


# ---------------------------------------------------------------- models
def cmd_models(args) -> int:
    from .config import model_cached
    from .stages.asr import MODEL_REPOS, MODEL_SIZES

    settings = load_settings()
    print("识别模型（Whisper）")
    for name, size in MODEL_SIZES.items():
        cached = model_cached(MODEL_REPOS.format(model=name))
        mark = "已缓存" if cached else f"需下载 {size} GB"
        star = " ← 当前" if name == settings.asr_model else ""
        print(f"  {name:<16} {mark}{star}")

    mt_cached = model_cached(settings.mt_local_model)
    print("\n翻译模型")
    print(f"  {settings.mt_local_model:<40} "
          f"{'已缓存' if mt_cached else '需下载 5.8 GB（可改用 API 翻译省掉）'}")

    if args.download:
        return _download_models(settings, with_mt=args.mt)
    return 0


def _download_models(settings: Settings, with_mt: bool) -> int:
    from .config import hf_env_for_model
    from .stages.asr import MODEL_REPOS

    targets = [MODEL_REPOS.format(model=settings.asr_model)]
    if with_mt:
        targets.append(settings.mt_local_model)

    for repo in targets:
        if not model_cached_safe(repo):
            hf_env_for_model(repo, settings.hf_mirror)
            log(f"下载 {repo} …（可能几分钟到几十分钟）")
            try:
                from huggingface_hub import snapshot_download
            except ImportError:
                log("缺少 huggingface_hub", "error")
                return 3
            snapshot_download(repo, allow_patterns=["*.json", "*.safetensors",
                                                    "*.txt", "*.model", "*.bin"])
        log(f"{repo} 就绪")
    return 0


def model_cached_safe(repo: str) -> bool:
    from .config import model_cached

    return model_cached(repo)


# ---------------------------------------------------------------- clean
def cmd_clean(args) -> int:
    from .jobs.store import cleanup, disk_usage

    before = disk_usage()["used_gb"]
    removed, freed = cleanup(args.days, keep_results=not args.all)
    after = disk_usage()["used_gb"]
    log(f"删除 {removed} 个任务，释放 {human_size(freed)}（{before} GB → {after} GB）")
    return 0


# ---------------------------------------------------------------- config
def cmd_config(args) -> int:
    p = config_path()
    if args.path:
        print(p)
        return 0
    if args.init:
        target = save_settings(load_settings())
        log(f"已写入 {target}")
        return 0
    if p.exists():
        print(p.read_text(encoding="utf-8"))
    else:
        log(f"还没有配置文件（{p}）。用 videodub config --init 生成一份。")
        log("当前全部使用默认值。")
    return 0


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # 不带参数 = 启动网页界面，这是产品的默认入口
        return cmd_serve(argparse.Namespace(host=None, port=None, no_browser=False))

    ap = _build_parser()
    args = ap.parse_args(argv)

    handlers = {
        "run": cmd_run, "serve": cmd_serve, "doctor": cmd_doctor,
        "voices": cmd_voices, "models": cmd_models, "clean": cmd_clean,
        "config": cmd_config,
    }
    if not args.cmd or args.cmd not in handlers:
        ap.print_help()
        return 0

    try:
        return handlers[args.cmd](args)
    except VideoDubError as exc:
        log(f"{exc.message}", "error")
        if exc.hint:
            log(f"提示：{exc.hint}", "warn")
        if exc.detail:
            log(f"详情：{exc.detail[-1500:]}", "debug")
        return exc.exit_code
    except KeyboardInterrupt:
        log("已中断", "warn")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
