"""API 层测试：用 FastAPI TestClient 验证端点逻辑（不带外网、不占端口、不碰真实配置）"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

H = {"host": "127.0.0.1:8787"}  # 绕过本机 Host 校验中间件


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # with 触发 lifespan（后台健康检查；无渠道时不发外网）
        yield c


def test_health(client):
    assert client.get("/health", headers=H).json() == {"ok": True}


def test_models_test_validation(client):
    r = client.post("/api/models/test", json={}, headers=H)
    assert r.status_code == 400
    assert "model" in r.json()["detail"]


def test_models_test_no_channel(client):
    # 没有配置任何渠道 → 404
    r = client.post("/api/models/test", json={"model": "glm-4-flash"}, headers=H)
    assert r.status_code == 404


def test_models_test_invalid_channel(client):
    # 指定不存在的渠道 → 友好失败而非 500
    r = client.post("/api/models/test",
                    json={"model": "glm-4-flash", "channel_id": "ch_nonexistent"},
                    headers=H)
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_gateway_auth_required(client):
    # /v1 接口需要 Bearer Token（本机无 token 时 401）
    r = client.post("/api/models/test", json={}, headers=H)  # 占位，真实断言在下面
    r = client.get("/v1/models", headers=H)
    assert r.status_code == 401
