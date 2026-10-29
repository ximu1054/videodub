"""识别阶段的错误解释逻辑（不需要加载模型）。

CUDA 缺 DLL 这个坑值得单独测：报错发生在**推理时**而不是加载时，
而且原文是 `Library cublas64_12.dll is not found or cannot be loaded`——
用户看到这句完全不知道该干什么。
"""

from __future__ import annotations

from videodub.stages.asr import (CUDA_DLL_HINT, MODEL_SIZES,
                                 _looks_like_cuda_dll_error,
                                 _looks_like_network_error, load_transcript,
                                 model_cached)


class _ProxyError(Exception):
    pass


class _RateLimit(Exception):
    pass


def test_network_error_recognised_by_name():
    """ProxyError 不能当成显卡问题——否则会先退回 CPU，然后在同一处再失败一次。"""
    assert _looks_like_network_error(_ProxyError("502 Bad Gateway")) is True


def test_network_error_recognised_by_module():
    exc = _RateLimit("boom")
    exc.__class__.__module__ = "httpx2._client"
    assert _looks_like_network_error(exc) is True


def test_network_error_message_marker():
    exc = ValueError("Cannot find the requested files in the cached path and outgoing "
                     "traffic was disabled, so we cannot find the file locally.")
    assert _looks_like_network_error(exc) is True


def test_real_gpu_errors_are_not_network_errors():
    assert _looks_like_network_error(
        RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    ) is False
    assert _looks_like_network_error(ValueError("bad shape")) is False


def test_recognises_cublas_error():
    exc = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    assert _looks_like_cuda_dll_error(exc) is True


def test_recognises_cudnn_error():
    assert _looks_like_cuda_dll_error(RuntimeError("Could not locate cudnn_ops64_9.dll"))


def test_ignores_unrelated_errors():
    assert _looks_like_cuda_dll_error(RuntimeError("out of memory")) is False
    assert _looks_like_cuda_dll_error(ValueError("bad input")) is False


def test_hint_mentions_both_ways_out():
    """提示必须给出两条路：装运行库，或者退回 CPU。"""
    assert "nvidia-cublas-cu12" in CUDA_DLL_HINT
    assert "--device cpu" in CUDA_DLL_HINT


def test_model_sizes_cover_builtin_choices():
    for name in ("tiny", "base", "small", "medium", "large-v3"):
        assert name in MODEL_SIZES
        assert MODEL_SIZES[name] > 0


def test_model_cached_false_for_missing(home, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert model_cached("large-v3") is False


def test_load_transcript_raises_when_missing(home, tmp_path):
    import pytest

    from videodub.errors import StageError

    with pytest.raises(StageError):
        load_transcript(tmp_path)
