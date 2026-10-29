"""人声分离（可选，实验性）。

**v1 不默认依赖 demucs**。原因很实在：demucs 4.1 的元数据钉着 ``torchaudio<2.1``，
和现代 torch 直接冲突，标准解析器会报 ResolutionImpossible。旧方案靠
``--no-deps`` 装本体再手补 5 个包才跑起来——这条路没法让开源用户走。

所以默认路径是「**你自己提供伴奏文件**」：用剪映、UVR5、或自己装的 demucs 分离出
伴奏 wav，拖进界面即可。这里保留自动分离，但要求用户显式装好依赖。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..errors import MissingDependencyError, StageError
from ..log import log
from ..media.ffmpeg import find_ffmpeg, run_ffmpeg

STAGE = "render"

INSTALL_HINT = (
    "自动分离需要 demucs，且它和老版本 torchaudio 有依赖冲突。推荐：\n"
    "  pip install --no-deps demucs==4.1.0\n"
    "  pip install sphn julius lameenc einops safetensors openunmix\n"
    "装不上就换个思路：用剪映/UVR5 自己分离出伴奏 wav，然后在界面上传进来。"
)


def available() -> bool:
    try:
        import demucs  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def separate(video, outdir: Path, model: str = "htdemucs") -> tuple[Path, Path]:
    """拆成伴奏与人声，返回 ``(bgm_wav, vocals_wav)``。"""
    if not available():
        raise MissingDependencyError("没有安装 demucs", hint=INSTALL_HINT)

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # demucs 吃 wav 最稳，先把音轨抽出来
    src = outdir / "for_separate.wav"
    if not src.exists():
        run_ffmpeg(["-y", "-v", "error", "-i", str(video), "-vn",
                    "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(src)],
                   stage=STAGE)

    log("正在分离人声（CPU 上长视频会比较慢）")
    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals",
           "-n", model, "-o", str(outdir), str(src)]
    proc = subprocess.run(cmd, capture_output=True, text=True,  # noqa: S603
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise StageError(
            stage=STAGE, message="demucs 分离失败",
            hint="把下面的 stderr 贴进 issue；或改用手动提供的伴奏文件",
            detail=(proc.stderr or "").strip()[-2000:],
        )

    stem = outdir / model / src.stem
    vocals = stem / "vocals.wav"
    bgm = stem / "no_vocals.wav"
    if not bgm.exists() or not vocals.exists():
        raise StageError(stage=STAGE, message="demucs 没有产出预期文件",
                         detail=str(stem))

    # 挪到固定名字，方便后续引用
    final_bgm = outdir / "bgm.wav"
    final_vocals = outdir / "vocals.wav"
    final_bgm.write_bytes(bgm.read_bytes())
    final_vocals.write_bytes(vocals.read_bytes())
    return final_bgm, final_vocals


__all__ = ["STAGE", "available", "separate", "INSTALL_HINT", "find_ffmpeg"]
