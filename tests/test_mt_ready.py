"""提交前的翻译后端体检。

这个检查存在的理由很具体：显式配了 ``backend=api`` 但忘记设 key 时，
旧行为要等识别跑完二十多秒才在翻译阶段报错——用户白等一场。
现在提交即失败。
"""

from __future__ import annotations

import pytest

from videodub.config import Settings
from videodub.errors import AuthError, MissingDependencyError
from videodub.stages.translate import check_ready


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("VIDEODUB_API_KEY", raising=False)
    monkeypatch.delenv("TRANSLATE_API_KEY", raising=False)


def test_api_backend_without_key_fails_fast():
    with pytest.raises(AuthError) as ei:
        check_ready(Settings(mt_backend="api"))
    assert ei.value.hint and "VIDEODUB_API_KEY" in ei.value.hint


def test_api_backend_with_key_ok():
    assert check_ready(Settings(mt_backend="api", mt_api_key="sk-x")) == "api"


def test_key_from_env_counts(monkeypatch):
    monkeypatch.setenv("VIDEODUB_API_KEY", "sk-env")
    assert check_ready(Settings(mt_backend="api")) == "api"


def test_none_backend_is_allowed():
    assert check_ready(Settings(mt_backend="none")) == "none"


def test_local_backend_without_torch_explains_how():
    result = Settings(mt_backend="local")
    try:
        import torch  # noqa: F401
    except ImportError:
        with pytest.raises(MissingDependencyError) as ei:
            check_ready(result)
        assert "local-mt" in (ei.value.hint or "")
    else:
        assert check_ready(result) == "local"


def test_auto_without_anything_suggests_both_roads():
    try:
        import torch  # noqa: F401
    except ImportError:
        with pytest.raises(MissingDependencyError) as ei:
            check_ready(Settings(mt_backend="auto"))
        hint = ei.value.hint or ""
        assert "VIDEODUB_API_KEY" in hint or "local-mt" in hint
    else:
        assert check_ready(Settings(mt_backend="auto")) == "local"


def test_auto_prefers_api_when_key_present(monkeypatch):
    monkeypatch.setenv("VIDEODUB_API_KEY", "sk-env")
    assert check_ready(Settings(mt_backend="auto")) == "api"
