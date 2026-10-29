"""Range 响应：``<video>`` 拖进度条全靠它。

错了不会报错，只会表现成「拖动卡住、整片重下」——这种问题最难排查，
所以用真实请求打一遍。
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from videodub.media.range import range_response

BODY = bytes(range(256)) * 40          # 10240 字节


@pytest.fixture
def media(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(BODY)
    return p


def req(range_header: str | None) -> Request:
    headers = [(b"range", range_header.encode())] if range_header else []
    return Request({"type": "http", "method": "GET", "path": "/x",
                    "headers": headers, "query_string": b""})


def body_of(resp) -> bytes:
    if hasattr(resp, "body_iterator"):
        chunks = []

        async def collect():
            async for c in resp.body_iterator:
                chunks.append(c)

        import asyncio

        asyncio.run(collect())
        return b"".join(chunks)
    return resp.body


def test_missing_file_404(tmp_path):
    resp = range_response(tmp_path / "nope.mp4", req(None))
    assert resp.status_code == 404


def test_no_range_returns_full_file(media):
    resp = range_response(media, req(None))
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"


def test_byte_range_206(media):
    resp = range_response(media, req("bytes=0-99"))
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-99/{len(BODY)}"
    assert resp.headers["content-length"] == "100"
    assert body_of(resp) == BODY[:100]


def test_open_ended_range(media):
    resp = range_response(media, req("bytes=10000-"))
    assert resp.status_code == 206
    assert body_of(resp) == BODY[10000:]


def test_suffix_range(media):
    """``bytes=-500`` 表示最后 500 字节。"""
    resp = range_response(media, req("bytes=-500"))
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {len(BODY) - 500}-{len(BODY) - 1}/{len(BODY)}"
    assert body_of(resp) == BODY[-500:]


def test_suffix_larger_than_file(media):
    resp = range_response(media, req("bytes=-999999"))
    assert resp.status_code == 206
    assert body_of(resp) == BODY


def test_out_of_range_416(media):
    resp = range_response(media, req(f"bytes={len(BODY)}-"))
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(BODY)}"


def test_reversed_range_416(media):
    resp = range_response(media, req("bytes=200-100"))
    assert resp.status_code == 416


def test_garbage_range_400(media):
    resp = range_response(media, req("items=0-10"))
    assert resp.status_code == 400


def test_end_clamped_to_size(media):
    resp = range_response(media, req("bytes=10000-999999"))
    assert resp.status_code == 206
    assert resp.headers["content-range"].endswith(f"/{len(BODY)}")
    assert body_of(resp) == BODY[10000:]


def test_media_type_from_extension(media):
    assert range_response(media, req("bytes=0-1")).media_type == "video/mp4"
