"""识别阶段的错误解释逻辑（不需要加载模型）。

CUDA 缺 DLL 这个坑值得单独测：报错发生在**推理时**而不是加载时，
而且原文是 `Library cublas64_12.dll is not found or cannot be loaded`——
用户看到这句完全不知道该干什么。
"""

from __future__ import annotations

from videodub.stages.asr import (CUDA_DLL_HINT, MODEL_SIZES,
                                 _looks_like_cuda_dll_error, load_transcript,
                                 model_cached)


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
