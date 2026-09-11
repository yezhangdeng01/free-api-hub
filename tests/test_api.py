"""API 层测试：用 FastAPI TestClient 验证端点逻辑（不带外网、不占端口、不碰真实配置）"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

H = {"host": "127.0.0.1:8787"}  # 绕过本机 Host 校验中间件


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """with 触发 lifespan（后台健康检查）。

    **落盘路径改到临时目录**：lifespan 退出时会 `save_runtime_state()`，不改的话
    测试状态会把用户真实的 `data/runtime_state.json`（冷却/硬失败/渠道评分）
    覆盖掉——尤其渠道评分现在是跨重启累计的，被冲掉等于「稳定优先」白学。"""
    from app import store as _store
    tmp = tmp_path_factory.mktemp("api-state")
    old = (_store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH)
    _store.RUNTIME_STATE_PATH = str(tmp / "runtime_state.json")
    _store.MODEL_STATUS_PATH = str(tmp / "model_status.json")
    try:
        with TestClient(app) as c:  # with 触发 lifespan，无渠道时不发外网
            yield c
    finally:
        _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH = old


def test_health(client):
    assert client.get("/health", headers=H).json() == {"ok": True}


def test_models_test_validation(client):
    r = client.post("/api/models/test", json={}, headers=H)
    assert r.status_code == 400
    assert "model" in r.json()["detail"]


def test_v1_models_usable_first(client, monkeypatch):
    """/v1/models 必须「能用的排前面」——auto 就是按这个顺序逐个试的。

    用假 model_view 隔离排序逻辑：受限但分高的模型必须排在可用但分低的之后。"""
    from app import config as cfgmod
    from app import gateway

    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [], "aliases": {}, "route_strategy": "quality",
        "pinned": [], "auth_enabled": False, "api_token": "", "port": 8787})
    monkeypatch.setattr(gateway, "list_reserved_auto", lambda: ["auto"])
    monkeypatch.setattr(gateway, "alias_view", lambda cfg: [])
    monkeypatch.setattr(gateway, "model_view", lambda cfg: [
        {"id": "a-limited-strong", "status": "limited", "tier": 3, "cap_score": 1.0,
         "channels": [{"available": True, "eff_score": 1.0, "latency_ms": 100}]},
        {"id": "b-ok-weak", "status": "ok", "tier": 1, "cap_score": 0.1,
         "channels": [{"available": True, "eff_score": 0.4, "latency_ms": 3000}]},
    ])
    ids = [m["id"] for m in client.get("/v1/models", headers=H).json()["data"]]
    assert ids == ["auto", "b-ok-weak", "a-limited-strong"], ids


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
