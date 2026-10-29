"""配置读写。这里最容易出「改了配置没生效」这种玄学问题。"""

from __future__ import annotations

import pytest

from videodub.config import (DEFAULTS, Settings, app_home, config_path, data_dir,
                             load_settings, model_cached, model_snapshot,
                             resolve_hf_endpoint, save_settings)


def test_defaults_cover_all_fields():
    s = Settings()
    for key in DEFAULTS:
        assert hasattr(s, key), f"Settings 缺字段 {key}"


def test_home_follows_env(home):
    assert app_home() == home.resolve()
    assert config_path().parent == home.resolve()
    assert data_dir() == home.resolve() / "jobs"


def test_load_returns_defaults_when_missing(home):
    s = load_settings()
    assert s.voice == DEFAULTS["voice"]
    assert s.max_tempo == DEFAULTS["max_tempo"]


def test_roundtrip_preserves_everything(home):
    """save → load 必须完全还原，包括 [mt.api] 这种二级段。"""
    s = Settings()
    s.voice = "zh-CN-YunxiNeural"
    s.rate = "+15%"
    s.max_tempo = 1.3
    s.offset = -0.25
    s.mt_api_base = "https://api.example.com/v1"
    s.mt_api_model = "some-model"
    s.port = 9000
    save_settings(s)

    back = load_settings()
    assert back.voice == "zh-CN-YunxiNeural"
    assert back.rate == "+15%"
    assert back.max_tempo == pytest.approx(1.3)
    assert back.offset == pytest.approx(-0.25)
    assert back.mt_api_base == "https://api.example.com/v1"
    assert back.mt_api_model == "some-model"
    assert back.port == 9000


def test_api_key_not_written_to_disk(home):
    """密钥不落盘，这是硬约定。"""
    s = Settings()
    s.mt_api_key = "sk-secret-should-not-appear"
    p = save_settings(s)
    assert "sk-secret" not in p.read_text(encoding="utf-8")


def test_api_key_from_env_wins(home, monkeypatch):
    monkeypatch.setenv("VIDEODUB_API_KEY", "sk-from-env")
    s = Settings()
    s.mt_api_key = "sk-from-file"
    save_settings(s)
    assert load_settings().mt_api_key == "sk-from-env"


def test_broken_toml_falls_back_to_defaults(home):
    config_path().write_text("this is not [valid toml", encoding="utf-8")
    assert load_settings().voice == DEFAULTS["voice"]


def test_flat_toml_accepted(home):
    config_path().write_text('voice = "zh-CN-YunjianNeural"\n', encoding="utf-8")
    assert load_settings().voice == "zh-CN-YunjianNeural"


def test_coerce_clamps_out_of_range():
    s = Settings(max_tempo=5.0, peak=3.0, tts_concurrency=99, asr_device="tpu")
    s._coerce()
    assert s.max_tempo == 2.0
    assert s.peak == 1.0
    assert s.tts_concurrency == 8
    assert s.asr_device == "auto"


def test_rate_percent_roundtrip():
    s = Settings()
    s.rate_percent = 12
    assert s.rate == "+12%"
    assert s.rate_percent == 12
    s.rate = "-5%"
    assert s.rate_percent == -5
    s.rate = "garbage"
    assert s.rate_percent == 0


def test_to_dict_has_no_extra():
    d = Settings().to_dict()
    assert "extra" not in d
    assert "voice" in d


def test_from_dict_ignores_unknown_keys():
    s = Settings.from_dict({"voice": "zh-CN-XiaoyiNeural", "nonsense": 1})
    assert s.voice == "zh-CN-XiaoyiNeural"


def test_resolve_hf_endpoint_modes(monkeypatch):
    # auto 会探测网络，这里只测确定性分支
    assert resolve_hf_endpoint("none") is None
    assert resolve_hf_endpoint("hf-mirror") == "https://hf-mirror.com"
    assert resolve_hf_endpoint("https://my.mirror") == "https://my.mirror"
    assert resolve_hf_endpoint("https://my.mirror/") == "https://my.mirror"


def test_model_cached_requires_snapshot(home, monkeypatch, tmp_path):
    """只有目录、没有快照 = 没下完。这种情况不能当「已缓存」，否则会切离线后加载失败。"""
    hub = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hub))
    assert model_cached("org/model") is False

    repo = hub / "hub" / "models--org--model"
    (repo / "blobs").mkdir(parents=True)
    assert model_cached("org/model") is False

    snap = repo / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    assert model_snapshot("org/model") == snap
    assert model_cached("org/model") is True
    assert model_cached("org/other") is False


def test_model_snapshot_follows_refs_main(home, monkeypatch, tmp_path):
    hub = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hub))
    repo = hub / "hub" / "models--org--model"
    old = repo / "snapshots" / "old"
    new = repo / "snapshots" / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("old\n", encoding="utf-8")
    assert model_snapshot("org/model") == old


def test_model_snapshot_falls_back_to_newest(home, monkeypatch, tmp_path):
    """没有 refs/main（老版本 hub 或手工拷贝）时，取最新的那个快照。"""
    import os
    import time

    hub = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hub))
    snaps = hub / "hub" / "models--org--model" / "snapshots"
    old = snaps / "old"
    new = snaps / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    assert model_snapshot("org/model") == new
