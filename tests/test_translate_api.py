"""API 翻译后端：请求构造、编号解析、错误码映射。

用本地假接口（``fixtures/stub_openai.py``）测，不联网、不花钱。
真接口要验的只有「对方是否守协议」，那不该由我们的测试来担保。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import stub_openai  # noqa: E402

from videodub.errors import AuthError, NetworkError  # noqa: E402
from videodub.stages.translate import ApiTranslator  # noqa: E402


@pytest.fixture
def stub():
    srv, base = stub_openai.serve_in_thread()
    stub_openai.handler.fail_with = 0
    yield base
    srv.shutdown()
    srv.server_close()


def make(base: str) -> ApiTranslator:
    return ApiTranslator(base_url=base, api_key="sk-test", model="stub-model")


def test_translate_batch_roundtrip(stub):
    t = make(stub)
    out = t.translate_batch(["hello", "world"], context="demo")
    assert out == ["中文：hello", "中文：world"]


def test_translate_batch_keeps_order_and_length(stub):
    items = [f"line {i}" for i in range(8)]
    out = make(stub).translate_batch(items)
    assert len(out) == 8
    assert out[0].endswith("line 0")
    assert out[-1].endswith("line 7")


def test_translate_one_short(stub):
    t = make(stub)
    assert t.translate_one_short("a fairly long sentence about agents", 20).startswith("中文：")


def test_missing_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("VIDEODUB_API_KEY", raising=False)
    monkeypatch.delenv("TRANSLATE_API_KEY", raising=False)
    with pytest.raises(AuthError) as ei:
        ApiTranslator(base_url="http://127.0.0.1:1/v1", api_key="", model="x")
    assert "key" in ei.value.message.lower() or "key" in (ei.value.hint or "").lower()


def test_401_maps_to_auth_error(stub):
    stub_openai.handler.fail_with = 401
    try:
        with pytest.raises(AuthError):
            make(stub).translate_batch(["hello"])
    finally:
        stub_openai.handler.fail_with = 0


def test_429_maps_to_network_error(stub):
    stub_openai.handler.fail_with = 429
    try:
        with pytest.raises(NetworkError) as ei:
            make(stub).translate_batch(["hello"])
        assert "429" in ei.value.message
    finally:
        stub_openai.handler.fail_with = 0


def test_unreachable_maps_to_network_error(monkeypatch):
    """URLError 要变成「连不上」而不是裸异常。

    直接连一个死端口在这里不可行——CI 和本地都可能被透明代理接管，
    返回的是 502 而不是连接失败。所以精确地制造 URLError。
    """
    import urllib.error
    import urllib.request

    def boom(*a, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(NetworkError) as ei:
        ApiTranslator(base_url="http://127.0.0.1:9/v1", api_key="k", model="m")._post("s", "u")
    assert "连不上" in ei.value.message
    assert ei.value.hint


def test_connection_reset_maps_to_network_error(monkeypatch):
    """连接被中途重置时要给可读提示，而不是一段 traceback。

    ConnectionResetError 是 OSError 的子类，不会被 urllib 包成 URLError——
    这个细节端到端跑一次就会撞上（服务端进程被 kill）。
    """
    import urllib.request

    def boom(*a, **kw):
        raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(NetworkError) as ei:
        ApiTranslator(base_url="http://127.0.0.1:9/v1", api_key="k", model="m")._post("s", "u")
    assert "连不上" in ei.value.message
    assert "ConnectionResetError" in (ei.value.detail or "")


def test_env_var_overrides_constructor(monkeypatch, stub):
    monkeypatch.setenv("VIDEODUB_API_BASE", stub)
    t = ApiTranslator(api_key="sk-x", model="stub-model")
    assert t.base == stub
