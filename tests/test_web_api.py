"""HTTP 接口的契约与防线。

重点不是「能不能返回 200」，而是三条防线：

  1. 非法 job_id / 文件名不能穿越到别的目录
  2. 跨站 Host / Origin 必须挡住（本地服务最容易被忽略的攻击面）
  3. SSE 的 ``id:`` 要能被 ``Last-Event-ID`` 接上
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from videodub.web.app import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(strict_host=True), base_url="http://127.0.0.1")


@pytest.fixture(scope="module")
def open_client():
    """不做 Host 校验的实例，用来测正常路径。"""
    return TestClient(create_app(strict_host=False), base_url="http://testserver")


# ---------------------------------------------------------------- 基础
def test_health(open_client):
    r = open_client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["version"]
    assert data["platform"] in ("windows", "macos", "linux")
    assert "asr" in data["models"]
    assert isinstance(data["voices"], list)
    assert data["voices"], "音色列表不能为空"
    assert data["voices"][0]["id"].startswith("zh-CN-")


def test_stats(open_client):
    r = open_client.get("/api/stats")
    assert r.status_code == 200
    for key in ("total", "done", "failed", "running", "queued", "used_gb"):
        assert key in r.json()


def test_index_page(open_client):
    r = open_client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_static_assets(open_client):
    for name in ("style.css", "app.js", "sse.js"):
        assert open_client.get(f"/static/{name}").status_code == 200


def test_jobs_list(open_client):
    r = open_client.get("/api/jobs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_favicon(open_client):
    assert open_client.get("/favicon.ico").status_code in (200, 204)


# ---------------------------------------------------------------- 路径防线
@pytest.mark.parametrize("bad", ["../../etc/passwd", "..%2f..%2fetc", "abc", "1"])
def test_bad_job_id_404(open_client, bad):
    assert open_client.get(f"/api/jobs/{bad}").status_code == 404


def test_missing_job_404(open_client):
    assert open_client.get("/api/jobs/20261029-103700-3f2a").status_code == 404


@pytest.mark.parametrize("name", ["../job.json", "..\\job.json", "%2e%2e%2fjob.json"])
def test_media_path_traversal_blocked(open_client, name):
    r = open_client.get(f"/api/media/20261029-103700-3f2a/{name}")
    assert r.status_code in (400, 404)


def test_create_job_requires_input(open_client):
    r = open_client.post("/api/jobs", data={"url": "", "params": "{}"})
    assert r.status_code == 400


def test_create_job_rejects_bad_params_json(open_client):
    r = open_client.post("/api/jobs", data={"url": "http://x", "params": "{oops"})
    assert r.status_code == 400


def test_create_job_rejects_unknown_extension(open_client):
    r = open_client.post(
        "/api/jobs",
        data={"url": "", "params": "{}"},
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "不支持" in r.json()["detail"]


# ---------------------------------------------------------------- Host/Origin
def test_host_check_blocks_foreign_host(client):
    r = client.get("/api/health", headers={"host": "evil.example.com"})
    assert r.status_code == 403


def test_host_check_allows_localhost(client):
    assert client.get("/api/health", headers={"host": "localhost:8765"}).status_code == 200
    assert client.get("/api/health", headers={"host": "127.0.0.1:8765"}).status_code == 200


def test_origin_check_blocks_cross_site(client):
    r = client.get("/api/health", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_origin_check_allows_same_origin(client):
    r = client.get("/api/health", headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


# ---------------------------------------------------------------- 语音试听
def test_preview_rejects_traversal(open_client):
    assert open_client.get("/api/preview/..%2f..%2fconfig.toml").status_code in (400, 404)


def test_job_text_404_without_file(open_client):
    assert open_client.get("/api/jobs/20261029-103700-3f2a/text").status_code == 404


# ---------------------------------------------------------------- 鲜度自检
def test_health_reports_stale_code(open_client, monkeypatch):
    """源码比进程新时必须报出来。

    页面的 css/js 是每次请求从磁盘读的，所以改完一刷新就变了；但 ``.py`` 在
    启动时已经编译进内存，不重启就还是旧的。用户看到的就是「界面明明变了，
    新功能却不出现」。这条让后端自己说出「需要重启」，别让人对着半新半旧的
    界面猜。
    """
    from videodub.web import api as web_api

    monkeypatch.setattr(web_api, "_STARTED_AT", 0.0)   # 假装进程启动于 1970
    monkeypatch.setattr(web_api, "_code_stale", False)

    assert open_client.get("/api/health").json()["code_stale"] is True


# ---------------------------------------------------------------- 开工前体检
def test_preflight_passes_through_reason(open_client, monkeypatch):
    """「能不能下载」的结论要原样带给前端，尤其是缺什么、该改哪里。"""
    from videodub.stages import download as dl
    from videodub.web import api as web_api

    monkeypatch.setattr(dl, "probe_access", lambda settings: {
        "ok": False, "reason": "ip-blocked",
        "message": "被拦的是节点出口 IP（1.2.3.4）", "cookies": "c.txt",
    })
    monkeypatch.setattr(web_api, "_PREFLIGHT", None, raising=False)

    r = open_client.post("/api/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "ip-blocked"


def test_preflight_caches_result(open_client, monkeypatch):
    """连点两次「开始配音」不该重跑一遍十几秒的探测。"""
    from videodub.stages import download as dl
    from videodub.web import api as web_api

    calls: list[int] = []

    def fake(settings):
        calls.append(1)
        return {"ok": True, "reason": "ok", "message": "", "cookies": ""}

    monkeypatch.setattr(dl, "probe_access", fake)
    monkeypatch.setattr(web_api, "_PREFLIGHT", None, raising=False)

    open_client.post("/api/preflight")
    open_client.post("/api/preflight")
    assert len(calls) == 1


def test_preflight_does_not_cache_failure(open_client, monkeypatch):
    """失败不缓存。

    用户被告知「cookies 没登录」之后，下一步就是去重新导出，回来立刻重试。
    如果失败也被缓存两分钟，他会以为自己的操作没生效。
    """
    from videodub.stages import download as dl
    from videodub.web import api as web_api

    calls: list[int] = []

    def fake(settings):
        calls.append(1)
        return {"ok": False, "reason": "cookies-not-logged-in",
                "message": "没登录", "cookies": ""}

    monkeypatch.setattr(dl, "probe_access", fake)
    monkeypatch.setattr(web_api, "_PREFLIGHT", None, raising=False)

    open_client.post("/api/preflight")
    open_client.post("/api/preflight")
    assert len(calls) == 2
