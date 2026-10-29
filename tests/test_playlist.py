"""合集（播放列表）路径。

三件事必须成立，否则「整个合集」会变成一个能骗过眼睛的假功能：

  1. 列表页能正确解析：集数、标题、序号，坏条目要跳过但序号不能错位
  2. 下载某一集时必须带 ``--playlist-items``，且**不能**再带 ``--no-playlist``
     （两者同时出现时 yt-dlp 只下第一个，是最容易静默出错的地方）
  3. 接口能把一个合集拆成 N 个任务，且尊重「最多处理前 N 集」

全部不联网：yt-dlp 的调用被打桩。
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from videodub.config import load_settings
from videodub.pipeline import sources_from_playlist
from videodub.stages import download as dl
from videodub.web import api as web_api
from videodub.web.app import create_app


# ---------------------------------------------------------------- 打桩
class _Run:
    """假的 ``subprocess.run`` 返回值。"""

    def __init__(self, stdout: str = "", code: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = code


def _playlist(count: int = 3, titles: list[str] | None = None) -> dict:
    titles = titles or [f"第 {i} 集" for i in range(1, count + 1)]
    return {
        "_type": "playlist",
        "id": "PLTEST",
        "title": "测试合集",
        "uploader": "Someone",
        "entries": [
            {"id": f"vid{i}", "title": t, "duration": 60.0 * i,
             "url": f"https://example.com/watch?v=vid{i}",
             "ie_key": "Youtube", "_type": "url"}
            for i, t in enumerate(titles, 1)
        ],
    }


@pytest.fixture
def settings():
    s = load_settings()
    s.proxy = "none"          # 不探测系统代理，避免拖慢测试
    return s


# ---------------------------------------------------------------- 探测
def test_probe_playlist_parses_entries(monkeypatch, settings):
    payload = json.dumps(_playlist(3))
    monkeypatch.setattr(dl.subprocess, "run", lambda *a, **k: _Run(payload))

    info = dl.probe_playlist("https://example.com/playlist?list=PLTEST", settings)

    assert info["ok"] and info["is_playlist"]
    assert info["count"] == 3 and info["total"] == 3
    assert info["title"] == "测试合集"
    assert [e["index"] for e in info["entries"]] == [1, 2, 3]
    assert info["entries"][0]["url"].endswith("vid1")


def test_probe_playlist_finds_current_episode(monkeypatch, settings):
    """``watch?v=xxx&list=yyy`` 要知道自己正停在第几集。"""
    payload = json.dumps(_playlist(5))
    monkeypatch.setattr(dl.subprocess, "run", lambda *a, **k: _Run(payload))

    info = dl.probe_playlist(
        "https://www.youtube.com/watch?v=vid3&list=PLTEST&index=3", settings
    )
    assert info["current_index"] == 3


def test_probe_playlist_skips_dead_entries_without_shifting_index(
    monkeypatch, settings
):
    """失效集要跳过，但后面各集的序号必须保持它在列表里的真实位置。

    序号一旦错位，``--playlist-items`` 就会去下隔壁那一集——错得不明显，
    但用户拿到的是别人的视频。
    """
    data = _playlist(3)
    data["entries"][0]["title"] = "[Private video]"
    monkeypatch.setattr(dl.subprocess, "run", lambda *a, **k: _Run(json.dumps(data)))

    info = dl.probe_playlist("https://example.com/playlist?list=PLTEST", settings)

    assert info["count"] == 2
    assert [e["index"] for e in info["entries"]] == [2, 3]


def test_probe_playlist_single_video(monkeypatch, settings):
    payload = json.dumps({"_type": "video", "id": "abc", "title": "单个视频",
                          "duration": 100, "uploader": "u"})
    monkeypatch.setattr(dl.subprocess, "run", lambda *a, **k: _Run(payload))

    info = dl.probe_playlist("https://example.com/watch?v=abc", settings)

    assert info["ok"] and not info["is_playlist"]
    assert info["title"] == "单个视频"
    assert info["duration"] == 100


def test_probe_playlist_reports_ytdlp_error(monkeypatch, settings):
    monkeypatch.setattr(dl.subprocess, "run",
                        lambda *a, **k: _Run(code=1, stderr="ERROR: 需要登录"))
    info = dl.probe_playlist("https://example.com/playlist?list=X", settings)
    assert not info["ok"]
    assert "需要登录" in info["error"]
    assert info["entries"] == []


# ---------------------------------------------------------------- 单集下载
class _Popen:
    def __init__(self, workdir):
        self.stdout = iter([])
        self.stderr = io.StringIO("")
        self.returncode = 0
        (workdir / "source.mp4").write_bytes(b"fake")

    def wait(self):
        return 0


def test_download_item_uses_playlist_items(tmp_path, monkeypatch, settings):
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _Popen(tmp_path)

    monkeypatch.setattr(dl.subprocess, "Popen", fake_popen)

    out = dl.download("https://example.com/playlist?list=PLTEST", tmp_path,
                      settings, item=3)

    assert out.name == "source.mp4"
    cmd = seen["cmd"]
    assert "--playlist-items" in cmd
    assert cmd[cmd.index("--playlist-items") + 1] == "3"
    # 两个参数同时出现时 yt-dlp 会忽略 --playlist-items，只下第一个
    assert "--no-playlist" not in cmd


def test_download_single_keeps_no_playlist(tmp_path, monkeypatch, settings):
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _Popen(tmp_path)

    monkeypatch.setattr(dl.subprocess, "Popen", fake_popen)

    dl.download("https://example.com/watch?v=abc", tmp_path, settings)

    assert "--no-playlist" in seen["cmd"]
    assert "--playlist-items" not in seen["cmd"]


# ---------------------------------------------------------------- 拆任务
def test_sources_from_playlist_respects_limit():
    info = {"id": "PLTEST", "title": "测试合集", "uploader": "u", "total": 3,
            "entries": [
                {"index": i, "title": f"第 {i} 集", "duration": 60.0,
                 "url": f"https://example.com/watch?v=v{i}"}
                for i in (1, 2, 3)
            ]}

    sources = sources_from_playlist(info, "https://example.com/playlist?list=PLTEST",
                                    limit=2)

    assert len(sources) == 2
    assert sources[0]["title"] == "第 1 集"
    assert sources[0]["playlist"]["index"] == 1
    assert sources[0]["playlist"]["total"] == 3
    assert "playlist_item" not in sources[0]


def test_sources_from_playlist_falls_back_to_item_index():
    """列表页没给直链时，退回「列表地址 + 第几集」。"""
    info = {"id": "PLTEST", "title": "t", "total": 1,
            "entries": [{"index": 7, "title": "某集", "duration": 10.0, "url": ""}]}

    src = sources_from_playlist(info, "https://example.com/playlist?list=PLTEST")[0]

    assert src["url"] == "https://example.com/playlist?list=PLTEST"
    assert src["playlist_item"] == 7


# ---------------------------------------------------------------- 接口
@pytest.fixture
def client(home, monkeypatch):
    """建任务接口：不真的跑流水线，只验证「拆成几个任务」。"""
    submitted: list[str] = []
    monkeypatch.setattr(web_api.state, "submit",
                        lambda job, settings: submitted.append(job.id) or 1)
    monkeypatch.setattr(web_api.translate, "check_ready", lambda settings: "none")
    monkeypatch.setattr(web_api, "_playlist_info",
                        lambda url, settings, refresh=False: {
                            "ok": True, "is_playlist": True, "id": "PLTEST",
                            "title": "测试合集", "uploader": "u", "count": 3,
                            "total": 3, "duration": 0.0, "thumbnail": "",
                            "current_index": 1, "error": "",
                            "entries": [
                                {"index": i, "title": f"第 {i} 集",
                                 "duration": 60.0, "url": f"https://e.com/v{i}"}
                                for i in (1, 2, 3)
                            ],
                        })
    c = TestClient(create_app(strict_host=False), base_url="http://testserver")
    c.submitted = submitted
    return c


def test_create_playlist_jobs(client):
    r = client.post("/api/jobs", data={
        "url": "https://example.com/playlist?list=PLTEST",
        "params": "{}", "scope": "playlist", "limit": "0",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["count"] == 3
    assert [j["playlist"]["index"] for j in data["jobs"]] == [1, 2, 3]
    assert len(client.submitted) == 3


def test_create_playlist_jobs_honours_limit(client):
    r = client.post("/api/jobs", data={
        "url": "https://example.com/playlist?list=PLTEST",
        "params": "{}", "scope": "playlist", "limit": "2",
    })
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    assert len(client.submitted) == 2


def test_create_playlist_rejects_single_video(client, monkeypatch):
    monkeypatch.setattr(web_api, "_playlist_info",
                        lambda url, settings, refresh=False: {
                            "ok": True, "is_playlist": False, "entries": [],
                            "title": "单个视频", "total": 0,
                        })
    r = client.post("/api/jobs", data={
        "url": "https://example.com/watch?v=abc",
        "params": "{}", "scope": "playlist",
    })
    assert r.status_code == 400
    assert "不是合集" in r.json()["detail"]
