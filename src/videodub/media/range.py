"""带 Range 支持的静态文件响应。

为什么需要自己写：``<video>`` 在浏览器里拖进度条会发 ``Range`` 请求，
标准库/框架的普通 FileResponse 若不支持 Range，Chrome 会退化成「整片重新下载」，
用户看到的现象是「进度条拖不动 + 卡」。

返回 206 + ``Content-Range``，按 64KB 分块 yield，避免把 138MB 读进内存。
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 64 * 1024


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def range_response(path: Path, request: Request) -> Response:
    path = Path(path)
    if not path.is_file():
        return Response(status_code=404)

    size = path.stat().st_size
    media_type = _media_type(path)
    raw = request.headers.get("range")
    if not raw:
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"}
        )

    m = _RANGE_RE.search(raw)
    if not m:
        return Response(status_code=400, headers={"Accept-Ranges": "bytes"})

    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return Response(status_code=400)

    if first == "":
        # bytes=-500 表示最后 500 字节
        length = min(int(last), size)
        start = max(size - length, 0)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
        end = min(end, size - 1)

    if start >= size or start > end:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )

    length = end - start + 1

    def _iter():
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _iter(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )
