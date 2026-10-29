"""处理阶段。每个阶段是独立函数，只依赖 Reporter 协议，不认识 Web 也不认识 CLI。"""

from __future__ import annotations

__all__ = [
    "align", "asr", "download", "prompts", "render", "segment", "translate", "tts", "units",
]
