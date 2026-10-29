"""异常族。

每个异常带 ``code``（机器可读，进 SSE 的 error 事件）和 ``exit_code``
（CLI 退出码，方便脚本判断失败类型）。

退出码约定：
  0    成功
  1    未分类错误
  2    用户输入有误（路径不存在、格式不支持、参数越界）
  3    缺少依赖（ffmpeg / torch / demucs）
  4    网络问题（下载失败、API 认证、TTS 连不上）
  5    媒体处理失败（ffmpeg 返回非 0）
  130  用户中断
"""

from __future__ import annotations


class VideoDubError(Exception):
    """所有本项目异常的基类。"""

    code = "GENERIC"
    exit_code = 1

    def __init__(self, message: str, *, hint: str = "", detail: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "detail": self.detail,
        }


class UserInputError(VideoDubError):
    code = "BAD_INPUT"
    exit_code = 2


class MissingDependencyError(VideoDubError):
    code = "MISSING_DEP"
    exit_code = 3


class NetworkError(VideoDubError):
    code = "NETWORK"
    exit_code = 4


class AuthError(NetworkError):
    code = "AUTH"


class DownloadError(NetworkError):
    code = "DOWNLOAD"

    def __init__(self, message: str, *, hint: str = "", detail: str = ""):
        if not hint:
            hint = (
                "最常见原因是 cookies 过期。用 Chrome 扩展 Get cookies.txt LOCALLY "
                "登录 YouTube 后重新导出，放到「下载」文件夹。"
            )
        super().__init__(message, hint=hint, detail=detail)


class StageError(VideoDubError):
    """某个处理阶段失败，附带该阶段的 stderr 片段。"""

    code = "STAGE"
    exit_code = 5

    def __init__(self, stage: str, message: str, *, hint: str = "", detail: str = ""):
        super().__init__(f"[{stage}] {message}", hint=hint, detail=detail)
        self.stage = stage

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["stage"] = self.stage
        return d


class MediaError(StageError):
    code = "MEDIA"

    def __init__(self, message: str, *, hint: str = "", detail: str = ""):
        super().__init__("render", message, hint=hint, detail=detail)


class Cancelled(VideoDubError):
    """用户主动取消。不是错误，但要能中断流水线。"""

    code = "CANCELLED"
    exit_code = 130

    def __init__(self, message: str = "已取消"):
        super().__init__(message)
