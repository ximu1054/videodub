"""任务存储：阶段状态判定、断点清理、路径校验。

``stage_state`` 那套「产物即真相」的判定是续跑能力的地基，
错一个分支就会出现「明明跑了却重跑一遍」或者「没跑却以为跑完了」。
"""

from __future__ import annotations

import json

import pytest

from videodub.errors import UserInputError
from videodub.jobs.store import (Job, cleanup, disk_usage, job_dir, list_jobs,
                                 running_jobs, validate_id)
from videodub.progress import STAGES


def make_job(**src) -> Job:
    source = {"kind": "file", "title": "t", "duration": 10.0, "playable": True}
    source.update(src)
    job = Job.create(source=source, params={})
    job.save()
    return job


# ---------------------------------------------------------------- 基础
def test_create_makes_workdir(home):
    job = make_job()
    assert job.workdir.is_dir()
    assert (job.workdir / "tmp").is_dir()
    assert (job.workdir / "job.json").is_file()


def test_validate_rejects_traversal(home):
    for bad in ("../etc", "20261029-103700-3f2a/../x", "", "abc", "2026102-103700-3f2a"):
        with pytest.raises(UserInputError):
            validate_id(bad)


def test_load_missing_raises(home):
    with pytest.raises(UserInputError):
        Job.load("20261029-103700-3f2a")


def test_roundtrip(home):
    job = make_job()
    job.state = "running"
    job.save()
    back = Job.load(job.id)
    assert back.id == job.id
    assert back.state == "running"
    assert back.title == "t"


def test_list_returns_all(home):
    a = make_job(title="a")
    b = make_job(title="b")
    ids = [j.id for j in list_jobs()]
    assert {a.id, b.id} <= set(ids)


def test_list_skips_garbage_dirs(home):
    make_job()
    (home / "jobs" / "not-a-job").mkdir()
    assert len(list_jobs()) == 1


# ---------------------------------------------------------------- 阶段状态
def test_initial_stages_pending(home):
    job = make_job()
    job.refresh_stages()
    for name in STAGES:
        if name == "download":
            assert job.stages[name]["state"] == "done"    # 本地文件没有下载
        else:
            assert job.stages[name]["state"] == "pending"


def test_stage_state_partial_when_intermediate_exists(home):
    job = make_job()
    (job.workdir / "tmp").mkdir(exist_ok=True)
    (job.workdir / "tmp" / "audio16k.wav").write_bytes(b"x")
    assert job.stage_state("asr") == "partial"


def test_stage_state_done_when_artifact_exists(home):
    job = make_job()
    (job.workdir / "transcript.json").write_text("[]", encoding="utf-8")
    assert job.stage_state("asr") == "done"


def test_render_done_needs_video(home):
    job = make_job()
    (job.workdir / "dubbed.mp4").write_bytes(b"x")
    assert job.stage_state("render") == "done"
    assert job.summary()["has_video"] is True


def test_url_job_download_partial_without_video(home):
    job = make_job(kind="url")
    (job.workdir / "source_url.txt").write_text("http://x", encoding="utf-8")
    assert job.stage_state("download") == "partial"
    (job.workdir / "source.mp4").write_bytes(b"x")
    assert job.stage_state("download") == "done"


def test_source_video_finds_any_container(home):
    job = make_job()
    assert job.source_video() is None
    (job.workdir / "source.mkv").write_bytes(b"x")
    assert job.source_video().name == "source.mkv"


# ---------------------------------------------------------------- 清理
def test_clear_from_keeps_translation_cache(home):
    """重跑 render 时要保留 translated.json——改几句文案不值得重花 API 钱。"""
    job = make_job()
    for name in ("dialogue.json", "translated.json", "dubbed.mp4"):
        (job.workdir / name).write_text("x", encoding="utf-8")
    job.clear_from("render")
    assert (job.workdir / "translated.json").exists()
    assert not (job.workdir / "dubbed.mp4").exists()


def test_clear_from_translate_removes_downstream(home):
    job = make_job()
    for name in ("transcript.json", "dialogue.json", "dubbed.mp4"):
        (job.workdir / name).write_text("x", encoding="utf-8")
    job.clear_from("translate")
    assert (job.workdir / "transcript.json").exists()     # 上游保留
    assert not (job.workdir / "dialogue.json").exists()
    assert not (job.workdir / "dubbed.mp4").exists()


def test_clear_from_unknown_stage(home):
    with pytest.raises(UserInputError):
        make_job().clear_from("nonsense")


def test_clean_tmp_keeps_results(home):
    job = make_job()
    (job.workdir / "tmp" / "x.wav").write_bytes(b"x")
    (job.workdir / "dubbed.mp4").write_bytes(b"x")
    job.clean_tmp()
    assert not (job.workdir / "tmp").exists()
    assert (job.workdir / "dubbed.mp4").exists()


def test_delete_removes_dir(home):
    job = make_job()
    job.delete()
    assert not job_dir(job.id).exists()


def test_cleanup_keeps_fresh_jobs(home):
    make_job()
    removed, _ = cleanup(days=7)
    assert removed == 0


def test_running_jobs_filter(home):
    a = make_job(title="a")
    b = make_job(title="b")
    a.state, b.state = "running", "done"
    a.save()
    b.save()
    assert [j.id for j in running_jobs()] == [a.id]


def test_disk_usage_counts_files(home):
    job = make_job()
    (job.workdir / "dubbed.mp4").write_bytes(b"0" * 1024)
    assert disk_usage()["used_gb"] >= 0


def test_summary_contract(home):
    job = make_job()
    s = job.summary()
    for key in ("id", "title", "state", "state_label", "created", "duration",
                "queue_position", "stages", "has_video", "has_audio", "size_mb",
                "report", "error", "playable"):
        assert key in s
    assert s["state_label"]


def test_job_json_is_human_readable(home):
    """用户贴 job.json 来报 bug 是常态，所以它必须是缩进过的 UTF-8。"""
    job = make_job(title="中文标题")
    raw = (job.workdir / "job.json").read_text(encoding="utf-8")
    assert "\n " in raw
    assert json.loads(raw)["title"] == "中文标题"
