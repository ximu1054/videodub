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
