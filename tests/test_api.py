"""API 层测试：用 FastAPI TestClient 验证端点逻辑（不带外网、不占端口、不碰真实配置）"""
import os
import sys
import time

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
    覆盖掉——尤其渠道评分现在是跨重启累计的，被冲掉等于「稳定优先」白学。

    ⚠️ **module 级夹具必须自己重定向**：pytest 先建 module 级夹具、后建 conftest 的
    函数级 autouse 夹具，所以 lifespan 里的 `store.init()` 拿到的是**生产路径**。
    2026-09-17 实测：给 usage 表加列的那句 ALTER 直接跑到了生产 `data/usage.db` 上
    （加了 superseded 列，无害，但证明这条路是通的）。四个落盘路径一个都不能漏。"""
    from app import store as _store
    tmp = tmp_path_factory.mktemp("api-state")
    old = (_store.DB_PATH, _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH,
           _store.CAPABILITY_CACHE_PATH)
    _store.DB_PATH = str(tmp / "usage.db")
    _store.RUNTIME_STATE_PATH = str(tmp / "runtime_state.json")
    _store.MODEL_STATUS_PATH = str(tmp / "model_status.json")
    # 能力榜单缓存同理：lifespan 退出时会 `capability.save_cache(force=True)`。
    # 2026-09-15 事故：这个 module 级夹具漏了它，而 conftest 的 autouse 夹具是函数级、
    # 在最后一个测试收尾时已经还原了路径 → 测试进程往**生产** capability_cache.json
    # 写了一份空缓存（bench={}）→ 用户重启后模型全部「没分了」。
    _store.CAPABILITY_CACHE_PATH = str(tmp / "capability_cache.json")
    try:
        with TestClient(app) as c:  # with 触发 lifespan，无渠道时不发外网
            yield c
    finally:
        (_store.DB_PATH, _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH,
         _store.CAPABILITY_CACHE_PATH) = old


def test_index_html_not_cached(client):
    """首页必须带 `no-store`：前端是现读磁盘的单文件，但 WebView2 会按启发式新鲜度用缓存，
    不给这个头时界面按 F5 也刷不出新版本（2026-09-17 用户报「改了前端必须重启服务」）。"""
    r = client.get("/", headers=H)
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", ""), dict(r.headers)
    assert "<html" in r.text.lower()


def test_chat_failover_records_superseded_not_failure(client, monkeypatch):
    """端到端：首个渠道失败 → 网关换路成功。**两次尝试各记一行，第一次仍算失败**，
    只是多标一个 `superseded=1`（这一跳之后换过渠道）。

    2026-09-17 用户口径：统计的是「具体这个模型的这一跳是失败还是成功还是其它」，
    所以换路成功不豁免失败的归属。"""
    from app import gateway, store
    from app import config as cfgmod
    from app import main as m

    model = "deepseek-ai/deepseek-v4-flash-0731"
    chans = [{"id": "t_c1", "name": "A", "type": "custom", "base_url": "http://a.invalid/v1",
              "api_key": "k", "enabled": True},
             {"id": "t_c2", "name": "B", "type": "custom", "base_url": "http://b.invalid/v1",
              "api_key": "k", "enabled": True}]
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    for c in chans:
        cs = gateway.ChannelState()
        cs.models, cs.valid, cs.latency_ms = [model], True, 10
        gateway.channels[c["id"]] = cs

    class FakeResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4}}

    class FakeClient:
        n = 0

        async def post(self, *a, **k):
            FakeClient.n += 1
            if FakeClient.n == 1:
                raise RuntimeError("Server disconnected without sending a response.")
            return FakeResp()

    monkeypatch.setattr(m, "shared_client", FakeClient())
    store.init()          # 每个测试的 DB_PATH 都是新的临时文件，表要自己建
    try:
        r = client.post("/v1/chat/completions",
                        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                        headers=H)
        assert r.status_code == 200, r.text
        logs = store.recent(limit=2)["logs"]
        assert logs[0]["success"] == 1 and logs[0]["superseded"] == 0      # 第二条候选：成功
        assert logs[1]["success"] == 0 and logs[1]["superseded"] == 1      # 第一条候选：失败 + 已换路
        t = store.summary(days=1)["totals"]
        assert t["errors"] == 1, t            # 失败归属不变：那一跳确实失败了
        assert t["superseded"] == 1, t        # 同时标出「后面换过路」
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)


def test_chat_entry_normalizes_model_through_dialect(client, monkeypatch):
    """chat 入口现在也走一遍 `dialects` 注册表：`model` 归一（去前后空白）后才交给 `_chat_v1`，
    回包形状不变。以前这个入口是直接调 `_chat_v1`，注册表里的 `chat` 条目没人用。"""
    from app import gateway, store
    from app import config as cfgmod
    from app import main as m

    model = "glm-test"
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [{"id": "t_c1", "name": "A", "type": "custom",
                      "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True}],
        "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    cs = gateway.ChannelState()
    cs.models, cs.valid, cs.latency_ms = [model], True, 10
    gateway.channels["t_c1"] = cs

    seen = []

    class FakeResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    class FakeClient:
        async def post(self, *a, **k):
            seen.append(k.get("json"))
            return FakeResp()

    monkeypatch.setattr(m, "shared_client", FakeClient())
    store.init()
    try:
        r = client.post("/v1/chat/completions", headers=H,
                        json={"model": f"  {model}  ",
                              "messages": [{"role": "user", "content": "ping"}]})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "pong"
        assert seen and seen[0]["model"] == model        # 归一之后才发上游
    finally:
        gateway.channels.pop("t_c1", None)


def test_chat_entry_bad_body_is_400_not_500(client, monkeypatch):
    """请求体不是 JSON 对象、或缺 model，都该是 400。

    旧写法 `_chat_v1(body, request)` 直接对 body 取 `.get`：传一个 JSON 数组进来会
    AttributeError → 500。现在由 `dialects.chat.to_chat` 统一挡成 400。"""
    from app import config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [], "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    r = client.post("/v1/chat/completions", headers=H, json=[1, 2])
    assert r.status_code == 400, r.text
    r = client.post("/v1/chat/completions", headers=H, json={"messages": []})
    assert r.status_code == 400, r.text
    assert "model" in r.json()["detail"]


def test_modelscope_quota_429_is_model_level_then_escalates(client, monkeypatch):
    """端到端（`_classify` 那条真实路径）：魔搭额度 429 **先只冷那一个模型**，
    渠道其余模型照常路由；30 分钟内撞到第 4 个不同模型才升级成「整渠道停到明天」。

    这是用户 2026-09-20 报的那个现象的回归：旧版一见 `insufficient balance` 就
    `mark_channel_quota_exhausted` → 一个模型撞一次，整条渠道封十几个小时。"""
    from app import gateway, store
    from app import config as cfgmod
    from app import main as m

    models = ["ms-m1", "ms-m2", "ms-m3", "ms-m4"]
    ch = {"id": "t_ms", "name": "魔搭 ModelScope", "type": "modelscope",
          "base_url": "http://ms.invalid/v1", "api_key": "k", "enabled": True}
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [ch], "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    cs = gateway.ChannelState()
    cs.models, cs.valid, cs.latency_ms = list(models), True, 10
    gateway.channels["t_ms"] = cs
    gateway.channel_cool.pop("t_ms", None)
    gateway.channel_quota_429.pop("t_ms", None)

    class R429:
        status_code = 429
        headers: dict = {}
        text = '{"error":{"message":"insufficient balance","request_id":"x"}}'

    class C:
        async def post(self, *a, **k):
            return R429()

    monkeypatch.setattr(m, "shared_client", C())
    store.init()
    try:
        for i, md in enumerate(models):
            r = client.post("/v1/chat/completions",
                            json={"model": md, "messages": [{"role": "user", "content": "hi"}]},
                            headers=H)
            assert r.status_code == 502, r.text
            assert gateway.cooldown.get((md, "t_ms"), 0) > 0      # 这个模型被冷却到明天
            if i < 3:
                assert not gateway.channel_cooling("t_ms"), f"第 {i+1} 个模型就封了整渠道"
                # 渠道其余模型照常进候选（这就是「模型级」的意义）
                assert gateway.candidates_for(models[i + 1], cfgmod.load_config())
            else:
                assert gateway.channel_cooling("t_ms")            # 第 4 个 → 判账号级
                assert gateway.candidates_for("ms-m1", cfgmod.load_config()) == []
        logged = [l for l in store.recent(limit=50)["logs"] if l["model"] == "ms-m1"]
        assert len(logged) == 1, f"一次 429 尝试只能记一行账，实际 {len(logged)} 行：{logged}"
    finally:
        gateway.channels.pop("t_ms", None)
        gateway.channel_cool.pop("t_ms", None)
        gateway.channel_quota_429.pop("t_ms", None)
        for md in models:
            gateway.cooldown.pop((md, "t_ms"), None)
            gateway.unverified.discard((md, "t_ms"))


def test_openrouter_free_daily_429_is_not_judged_as_no_balance(client, monkeypatch):
    """OpenRouter 免费模型（`:free`）的**每日额度** 429 不该被判成「余额不足 → 永久 down」。

    2026-09-21 实测的误判链条（三个条件叠在一起）：
      1. OR 账户是免费层、从未充值 → `/api/v1/credits` 返回 `total_credits=0,
         total_usage=0.2026` → `check_quota` 算出 `remaining=-0.2026` →
         `_quota_exhausted(cid)` **恒为 True**；
      2. 免费模型的日额度 429 正文是
         `{"error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits
         to unlock 1000 free model requests per day",...}}`；
      3. 旧代码让「余额接口说 0」**覆盖**正文分类 → 走 paid_balance 分支 →
         `mark_model_status(..., state="down")` + `mark_channel_down` + `cs.valid=False`。
    结果：一个**每天都会自己恢复**的额度限制被记成永久不可用（down 没有出口 → 再也不回来）。

    正解：`free_daily` 的正文证据比余额推断更具体 —— 正文说「日额度」，就按日额度处理。
    """
    from app import gateway, store
    from app import config as cfgmod
    from app import main as m

    md = "z-ai/glm-5.2:free"
    ch = {"id": "t_or", "name": "OpenRouter", "type": "openrouter",
          "base_url": "https://openrouter.ai/api/v1", "api_key": "k", "enabled": True}
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [ch], "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    cs = gateway.ChannelState()
    cs.models, cs.valid, cs.latency_ms = [md], True, 10
    # 关键前置：余额接口说「0 credits」——免费层就是这个值，免费模型并不需要它
    cs.quota = {"kind": "credits", "label": "账户余额", "total": 0.0,
                "used": 0.2026, "remaining": -0.2026, "unit": "USD"}
    gateway.channels["t_or"] = cs
    gateway.mark_model_status(md, True, "", "OpenRouter")     # 先干净地记成可用
    for d in (gateway.channel_cool, gateway.channel_quota_429, gateway.channel_down):
        d.pop("t_or", None)

    class R429:
        status_code = 429
        headers: dict = {}
        text = ('{"error":{"message":"Rate limit exceeded: free-models-per-day. '
                'Add 10 credits to unlock 1000 free model requests per day","code":429}}')

    class C:
        async def post(self, *a, **k):
            return R429()

    monkeypatch.setattr(m, "shared_client", C())
    store.init()
    try:
        r = client.post("/v1/chat/completions",
                        json={"model": md, "messages": [{"role": "user", "content": "hi"}]},
                        headers=H)
        assert r.status_code == 502, r.text
        st = gateway.model_status[md]
        assert st["state"] == "limited", f"被判成 {st['state']}（应为 limited）：{st.get('reason')}"
        assert st["available"] is False
        assert (md, "t_or") not in gateway.channel_down, "误记了 channel_down（永久不可用）"
        assert gateway.channels["t_or"].valid is True, "误导 cs.valid=False（等于把整条渠道停掉）"
        assert gateway.cooldown.get((md, "t_or"), 0) > 0, "该模型仍应冷却到明天"
        # 单模型撞额度 ≠ 渠道停摆（渠道级升级只在 30 分钟内 ≥4 个不同模型时发生）
        assert not gateway.channel_cooling("t_or")
        # 手动测试也不再被 down 挡住（方案 B）：能拿到候选才救得回来
        assert gateway.candidates_for_test(md, cfgmod.load_config())
    finally:
        gateway.channels.pop("t_or", None)
        gateway.channel_cool.pop("t_or", None)
        gateway.channel_quota_429.pop("t_or", None)
        gateway.channel_down.pop((md, "t_or"), None)
        gateway.cooldown.pop((md, "t_or"), None)
        gateway.unverified.discard((md, "t_or"))
        gateway.model_status.pop(md, None)


def test_models_test_hits_upstream_even_when_channel_cooled(client, monkeypatch):
    """渠道级冷却（魔搭额度用尽停到明天）时点「测试」，**仍然真发一次上游请求**，
    不是拿冷却状态糊弄用户——`candidates_for_test` 故意绕过 channel_cool。

    顺带守住自愈：测成功 → mark_result ok → 解除渠道冷却。"""
    from app import gateway
    from app import config as cfgmod
    from app import main as m

    ch = {"id": "t_cool", "name": "魔搭 ModelScope", "type": "modelscope",
          "base_url": "http://ms.invalid/v1", "api_key": "k", "enabled": True}
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [ch], "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    cs = gateway.ChannelState()
    cs.models, cs.valid, cs.latency_ms = ["ms-x"], True, 10
    gateway.channels["t_cool"] = cs
    gateway.channel_cool["t_cool"] = time.time() + 6 * 3600      # 冷却到明天
    calls = []

    class OK:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class C:
        async def post(self, url, **k):
            calls.append(url)
            return OK()

    monkeypatch.setattr(m, "shared_client", C())
    try:
        r = client.post("/api/models/test", json={"model": "ms-x"}, headers=H)
        assert r.status_code == 200, r.text
        assert len(calls) == 1, "渠道在冷却里就被拦下了 —— 用户点的测试应当真的发一次上游"
        assert r.json()["available"] is True, r.json()
        assert not gateway.channel_cooling("t_cool")             # 测成功 → 冷却解除
    finally:
        gateway.channels.pop("t_cool", None)
        gateway.channel_cool.pop("t_cool", None)
        gateway.cooldown.pop(("ms-x", "t_cool"), None)
        gateway.unverified.discard(("ms-x", "t_cool"))


def test_models_test_hits_upstream_even_when_channel_down(client, monkeypatch):
    """`channel_down`（硬失败）的模型点「测试」也**必须真的发一次上游请求**。

    这是 2026-09-21 用户拍板的方案 B（对应那条「down 没有出口」的机制死锁）：
    误判成 down 的模型，唯一能救回它的就是一次真实成功调用；而从前所有入口都被
    `channel_down` 自己挡着（自动路由排除、模型页测试也排除）→ 永远救不回来。
    所以手动测试特意穿透它。真·硬失败（余额/已下线）点几次还是同一句错，代价可控。
    """
    from app import gateway
    from app import config as cfgmod
    from app import main as m

    ch = {"id": "t_dn", "name": "OpenRouter", "type": "openrouter",
          "base_url": "https://openrouter.ai/api/v1", "api_key": "k", "enabled": True}
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [ch], "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    cs = gateway.ChannelState()
    cs.models, cs.valid, cs.latency_ms = ["or-x"], True, 10
    gateway.channels["t_dn"] = cs
    gateway.mark_channel_down("or-x", "t_dn", "HTTP 403")        # 旧记录：硬失败
    calls = []

    class OK:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class C:
        async def post(self, url, **k):
            calls.append(url)
            return OK()

    monkeypatch.setattr(m, "shared_client", C())
    try:
        r = client.post("/api/models/test", json={"model": "or-x"}, headers=H)
        assert r.status_code == 200, r.text
        assert len(calls) == 1, "被 channel_down 拦下了 —— 那这个模型就永远出不来"
        assert r.json()["available"] is True, r.json()
        assert ("or-x", "t_dn") not in gateway.channel_down   # 测成功 → 硬失败记录被清除
    finally:
        gateway.channels.pop("t_dn", None)
        gateway.channel_down.pop(("or-x", "t_dn"), None)
        gateway.cooldown.pop(("or-x", "t_dn"), None)
        gateway.unverified.discard(("or-x", "t_dn"))
        gateway.model_status.pop("or-x", None)


def test_models_test_multi_channel_stops_at_first_success(client, monkeypatch):
    """一个模型同时挂在两条渠道上时点「测试」，**按策略顺序逐个试、第一个成功即返回** ——
    不是把每条渠道都体检一遍（那是渠道卡片上「扫描全部模型」的活）。

    所以界面 toast 里「✔ 模型 可用 · 渠道名」的那个渠道名，就是真正被打到的那一条；
    后续渠道一个请求都不会发、也不会被写状态。
    """
    from app import gateway
    from app import config as cfgmod
    from app import main as m

    chans = [{"id": "t_ma", "name": "甲渠道", "type": "custom",
              "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True},
             {"id": "t_mb", "name": "乙渠道", "type": "custom",
              "base_url": "http://b.invalid/v1", "api_key": "k", "enabled": True}]
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    for c in chans:
        cs = gateway.ChannelState()
        cs.models, cs.valid, cs.latency_ms = ["dup-x"], True, 10
        gateway.channels[c["id"]] = cs
    calls = []

    class OK:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class C:
        async def post(self, url, **k):
            calls.append(url)
            return OK()

    monkeypatch.setattr(m, "shared_client", C())
    try:
        r = client.post("/api/models/test", json={"model": "dup-x"}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["available"] is True, r.json()
        assert len(calls) == 1, f"第一条就通了，不该再去打其它渠道：{calls}"
        hit = chans[0] if "a.invalid" in calls[0] else chans[1]
        assert r.json()["channel"] == hit["name"], r.json()
        for c in chans:                       # 没被请求的那条：一个状态都不许写
            if c is hit:
                continue
            assert ("dup-x", c["id"]) not in gateway.channel_down
            assert not gateway.cooldown.get(("dup-x", c["id"]))
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)
            gateway.cooldown.pop(("dup-x", c["id"]), None)
            gateway.unverified.discard(("dup-x", c["id"]))
            gateway.channel_down.pop(("dup-x", c["id"]), None)
        gateway.model_status.pop("dup-x", None)


def test_models_test_multi_channel_walks_past_model_level_limit(client, monkeypatch):
    """第一条渠道上该模型额度用完（模型级 `free_daily` 429）→ 测试**继续试第二条**，
    并返回真正通的那条渠道。

    这是「额度 429 降级到模型级」的另一面：既然是模型级，同一个模型换个渠道可能还能用，
    所以测试端点不能在第一跳就下结论。旧版在 free_daily 分支直接 return，
    会把「别处可用」误报成不可用（2026-09-20 一并修掉）。
    """
    from app import gateway, store
    from app import config as cfgmod
    from app import main as m

    chans = [{"id": "t_qa", "name": "甲渠道", "type": "modelscope",
              "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True},
             {"id": "t_qb", "name": "乙渠道", "type": "modelscope",
              "base_url": "http://b.invalid/v1", "api_key": "k", "enabled": True}]
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    for c in chans:
        cs = gateway.ChannelState()
        cs.models, cs.valid, cs.latency_ms = ["dup-q"], True, 10
        gateway.channels[c["id"]] = cs
        gateway.channel_cool.pop(c["id"], None)
        gateway.channel_quota_429.pop(c["id"], None)
    calls = []

    class R429:
        status_code = 429
        headers: dict = {}
        text = '{"error":{"message":"insufficient balance","request_id":"x"}}'

    class OK:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class C:
        async def post(self, url, **k):
            calls.append(url)
            return R429() if len(calls) == 1 else OK()   # 第一条额度用完，第二条好使

    monkeypatch.setattr(m, "shared_client", C())
    store.init()
    try:
        r = client.post("/api/models/test", json={"model": "dup-q"}, headers=H)
        assert r.status_code == 200, r.text
        assert len(calls) == 2, f"第一条 429 后就该继续试第二条：{calls}"
        j = r.json()
        assert j["available"] is True, j
        first = chans[0] if "a.invalid" in calls[0] else chans[1]
        second = chans[1] if first is chans[0] else chans[0]
        assert j["channel"] == second["name"], f"报回的应是真正通的那条：{j}"
        assert gateway.cooldown.get(("dup-q", first["id"]), 0) > 0   # 第一条按模型级冷了
        assert not gateway.channel_cooling(first["id"]), "一个模型不该把整条渠道停掉"
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)
            gateway.channel_cool.pop(c["id"], None)
            gateway.channel_quota_429.pop(c["id"], None)
            gateway.cooldown.pop(("dup-q", c["id"]), None)
            gateway.unverified.discard(("dup-q", c["id"]))
            gateway.channel_down.pop(("dup-q", c["id"]), None)
        gateway.model_status.pop("dup-q", None)


def test_models_test_multi_channel_walks_past_hard_failure(client, monkeypatch):
    """一条渠道**硬失败**（402 余额不足）→ 测试继续试第二条，返回真正通的那条。

    2026-09-21 用户口径：一个渠道失败就该测另一个，**所有渠道都失败**才报模型不可用。
    旧写法是 `if hard or cid: return` —— 一条渠道 402 就立刻返回，于是
    `z-ai/glm-5.3-flash` 点一次测试只拿到 OpenRouter 的 402（还白等 122s），
    而它其实在 NIM 上可用：界面报「不可用」、agent 调用却是好的，两边口径打架。

    死渠道的教训不丢：它仍被 `mark_channel_down` 记住，agent 调用/自动路由会跳过它 ——
    「这一条不用」和「这个模型不用」从此分开记。
    """
    from app import gateway
    from app import config as cfgmod
    from app import main as m

    chans = [{"id": "t_ha", "name": "甲渠道", "type": "custom",
              "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True},
             {"id": "t_hb", "name": "乙渠道", "type": "custom",
              "base_url": "http://b.invalid/v1", "api_key": "k", "enabled": True}]
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    for c in chans:
        cs = gateway.ChannelState()
        cs.models, cs.valid, cs.latency_ms = ["dup-h"], True, 10
        gateway.channels[c["id"]] = cs
    calls = []

    class R402:
        status_code = 402
        headers: dict = {}
        text = '{"error":{"message":"insufficient credits"}}'

    class OK:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class C:
        async def post(self, url, **k):
            calls.append(url)
            return R402() if len(calls) == 1 else OK()   # 第一条硬失败，第二条好使

    monkeypatch.setattr(m, "shared_client", C())
    try:
        r = client.post("/api/models/test", json={"model": "dup-h"}, headers=H)
        assert r.status_code == 200, r.text
        assert len(calls) == 2, f"第一条硬失败后就该继续试第二条：{calls}"
        j = r.json()
        assert j["available"] is True, j
        first = chans[0] if "a.invalid" in calls[0] else chans[1]
        second = chans[1] if first is chans[0] else chans[0]
        assert j["channel"] == second["name"], f"报回的应是真正通的那条：{j}"
        assert ("dup-h", first["id"]) in gateway.channel_down, "死渠道要记住，agent 调用才会跳过"
        assert gateway.model_status.get("dup-h", {}).get("state") == "ok", \
            "活渠道测通 → 模型整体仍是可用，不能被前一条死渠道带红"
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)
            gateway.cooldown.pop(("dup-h", c["id"]), None)
            gateway.unverified.discard(("dup-h", c["id"]))
            gateway.channel_down.pop(("dup-h", c["id"]), None)
        gateway.model_status.pop("dup-h", None)


def test_models_test_all_channels_fail_reports_every_attempt(client, monkeypatch):
    """所有渠道都失败 → 返回值带 `attempts`，逐条列出试过哪几条渠道。

    2026-09-21 口径改动的配套：既然改成「试完所有渠道才报不可用」，只报最后一条
    会让人以为「只试了一条」。界面靠这个字段把经过完整显示出来。
    """
    from app import gateway
    from app import config as cfgmod
    from app import main as m

    chans = [{"id": "t_aa", "name": "甲渠道", "type": "custom",
              "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True},
             {"id": "t_ab", "name": "乙渠道", "type": "custom",
              "base_url": "http://b.invalid/v1", "api_key": "k", "enabled": True}]
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    for c in chans:
        cs = gateway.ChannelState()
        cs.models, cs.valid, cs.latency_ms = ["dup-a"], True, 10
        gateway.channels[c["id"]] = cs
    calls = []

    class R402:
        status_code = 402
        headers: dict = {}
        text = '{"error":{"message":"insufficient credits"}}'

    class C:
        async def post(self, url, **k):
            calls.append(url)
            return R402()          # 两条渠道都硬失败

    monkeypatch.setattr(m, "shared_client", C())
    try:
        r = client.post("/api/models/test", json={"model": "dup-a"}, headers=H)
        assert r.status_code == 200, r.text
        assert len(calls) == 2, f"两条渠道都该试：{calls}"
        j = r.json()
        assert j["available"] is False, j
        got = [a["channel"] for a in j.get("attempts") or []]
        assert sorted(got) == sorted(c["name"] for c in chans), f"该列出全部尝试：{j}"
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)
            gateway.cooldown.pop(("dup-a", c["id"]), None)
            gateway.unverified.discard(("dup-a", c["id"]))
            gateway.channel_down.pop(("dup-a", c["id"]), None)
        gateway.model_status.pop("dup-a", None)


def test_candidates_for_test_puts_live_channel_first(monkeypatch):
    """手动测试候选的**两段式排序**：未记硬失败的渠道在前，组内才按策略综合分。

    2026-09-21（用户拍板）：此前是纯综合分排序，死渠道会排到活渠道前面 ——
    `z-ai/glm-5.3-flash` 的 OpenRouter 因「没有真实样本 → 稳定维按先验」而分数虚高，
    点一次测试先在死渠道上白等 122s 才拿到那句 402。

    这里刻意把死渠道的延迟调到 1ms（速度分必然更高），断言它**仍排在活渠道之后** ——
    证明分组优先于分数。同时断言死的没被丢掉：Plan B 的出口还在（活的都失败后
    仍会轮到他，用来确认它是否已恢复）。
    """
    from app import gateway
    from app import config as cfgmod

    chans = [{"id": "t_oa", "name": "甲渠道", "type": "custom",
              "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True},
             {"id": "t_ob", "name": "乙渠道", "type": "custom",
              "base_url": "http://b.invalid/v1", "api_key": "k", "enabled": True}]
    cfg = {"channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
           "auth_enabled": False, "api_token": "", "port": 8787}
    monkeypatch.setattr(cfgmod, "load_config", lambda: cfg)
    try:
        for i, c in enumerate(chans):
            cs = gateway.ChannelState()
            # 甲（死）：延迟 1ms → 综合分更高；乙（活）：9999ms → 分更低
            cs.models, cs.valid, cs.latency_ms = ["dup-o"], True, (1 if i == 0 else 9999)
            gateway.channels[c["id"]] = cs
        gateway.mark_channel_down("dup-o", "t_oa", "HTTP 402")
        order = [c["channel"]["id"] for c in gateway.candidates_for_test("dup-o", cfg)]
        assert order == ["t_ob", "t_oa"], f"活渠道必须在前，哪怕死渠道分更高：{order}"
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)
            gateway.cooldown.pop(("dup-o", c["id"]), None)
            gateway.unverified.discard(("dup-o", c["id"]))
            gateway.channel_down.pop(("dup-o", c["id"]), None)
        gateway.model_status.pop("dup-o", None)


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
    monkeypatch.setattr(gateway, "list_reserved_auto", lambda: ["auto-balanced"])
    monkeypatch.setattr(gateway, "alias_view", lambda cfg: [])
    monkeypatch.setattr(gateway, "model_view", lambda cfg: [
        {"id": "a-limited-strong", "status": "limited", "tier": 3, "cap_score": 1.0,
         "channels": [{"available": True, "stab": 1.0, "latency_ms": 100}]},
        {"id": "b-ok-weak", "status": "ok", "tier": 1, "cap_score": 0.1,
         "channels": [{"available": True, "stab": 0.4, "latency_ms": 3000}]},
    ])
    ids = [m["id"] for m in client.get("/v1/models", headers=H).json()["data"]]
    assert ids == ["auto-balanced", "b-ok-weak", "a-limited-strong"], ids


def test_pins_vision_scope_is_separate_list(client, monkeypatch):
    """收藏分两套（2026-09-20）：`list=vision` 写 `pinned_vision`，主收藏一点不动。

    语义：视觉视图/auto-vision 用自己那份收藏，互不干扰（同一个模型要两边各收一次）。
    """
    from app import config as cfgmod

    fake = {"channels": [], "aliases": {}, "route_strategy": "balanced", "pinned": [],
            "pinned_vision": [], "auth_enabled": False, "api_token": "", "port": 8787}
    monkeypatch.setattr(cfgmod, "load_config", lambda: fake)
    monkeypatch.setattr(cfgmod, "save_config", lambda c: None)

    # 默认（不带 list）= 主收藏
    r = client.post("/api/pins", json={"model": "m-main", "pinned": True}, headers=H)
    assert r.status_code == 200 and r.json()["list"] == "pinned"
    assert fake["pinned"] == ["m-main"] and fake["pinned_vision"] == []

    # list=vision → 只写视觉那份
    r = client.post("/api/pins", json={"model": "m-vis", "pinned": True, "list": "vision"}, headers=H)
    assert r.status_code == 200 and r.json()["list"] == "pinned_vision"
    assert fake["pinned_vision"] == ["m-vis"] and fake["pinned"] == ["m-main"]

    # 取消也分表：取消视觉的不能动主收藏
    client.post("/api/pins", json={"model": "m-vis", "pinned": False, "list": "vision"}, headers=H)
    assert fake["pinned_vision"] == [] and fake["pinned"] == ["m-main"]

    assert client.post("/api/pins", json={"pinned": True}, headers=H).status_code == 400


def test_chat_auto_sticky_keeps_same_model_within_conversation(client, monkeypatch, caplog):
    """端到端：**同一个会话里模型健康就不换模型**（2026-09-20 用户报「正常输出却换模型」）。

    四次请求串成一条链，互为对照：
      ① 新会话 → 按策略排序挑第一个；
      ② 把「另一个」设成收藏（按原排序会先试它）→ 同一会话**必须还是原来那个**（粘性压过收藏）；
      ③ 换个会话 → 回到收藏排序（证明②里那个收藏确实会生效，不是排序碰巧）；
      ④ 清掉粘性 → 同一会话重新按收藏排序（兜底手段可用）。
    另外断言每个 auto 请求都留一行「auto 选路[…]」（排障入口：为什么用了这个模型）。
    """
    from app import config as cfgmod
    from app import gateway, main as m, store

    import logging
    caplog.set_level(logging.INFO, logger="api-hub")   # 默认只收 WARNING，INFO 要显式放开

    models = ["sticky-fast", "pinned-big"]
    chans = [{"id": "t_st1", "name": "S1", "type": "custom", "base_url": "http://s1.invalid/v1",
              "api_key": "k", "enabled": True},
             {"id": "t_st2", "name": "S2", "type": "custom", "base_url": "http://s2.invalid/v1",
              "api_key": "k", "enabled": True}]
    state = {"pinned": []}
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "quality",
        "pinned": state["pinned"], "pinned_vision": [], "auth_enabled": False,
        "api_token": "", "port": 8787})
    for cfg_d, mdl, lat in ((chans[0], "sticky-fast", 10), (chans[1], "pinned-big", 3000)):
        cs = gateway.ChannelState()
        cs.models, cs.valid, cs.latency_ms = [mdl], True, lat
        gateway.channels[cfg_d["id"]] = cs

    sent = []

    class FakeResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4}}

    class FakeClient:
        async def post(self, url, json=None, headers=None):
            sent.append(json["model"])
            return FakeResp()

    def ask(msgs):
        r = client.post("/v1/chat/completions",
                        json={"model": "auto-quality", "messages": msgs}, headers=H)
        assert r.status_code == 200, r.text
        return sent[-1]

    monkeypatch.setattr(m, "shared_client", FakeClient())
    store.init()
    gateway.sticky.clear()
    conv_a = [{"role": "user", "content": "帮我把这份代码重构一下"}]
    try:
        caplog.clear()
        first = ask(conv_a)                                   # ① 新会话
        assert first in models, first
        other = "pinned-big" if first == "sticky-fast" else "sticky-fast"
        assert gateway.sticky_view(), "成功产出后应当记下会话粘性"
        assert "auto 选路[auto-quality]" in caplog.text, caplog.text      # 排障日志在（格式串没写错）

        state["pinned"] = [other]                             # ② 收藏另一个 → 粘性仍赢
        caplog.clear()
        again = ask(conv_a + [{"role": "assistant", "content": "hi"},
                              {"role": "user", "content": "继续"}])
        assert again == first, f"同一会话不该换模型：第一次 {first}，第二次 {again}"
        assert [v["model"] for v in gateway.sticky_view()] == [first]
        assert "沿用本会话上次成功的模型" in caplog.text, caplog.text

        conv_b = [{"role": "user", "content": "另一个毫不相干的任务"}]   # ③ 新会话
        assert ask(conv_b) == other, "新会话应当按收藏排序"

        st = client.get("/api/sticky", headers=H).json()
        assert len(st["sticky"]) == 2, st          # 两个会话各粘一个
        assert client.delete("/api/sticky?strategy=quality", headers=H).json()["cleared"] == 2
        assert ask(conv_a) == other, "清掉粘性后应当回到收藏排序"      # ④ 兜底
    finally:
        for c in chans:
            gateway.channels.pop(c["id"], None)
        gateway.sticky.clear()


def test_model_tier_manual_set_and_clear(client, monkeypatch):
    """点档位 chip 手动指定：写进 config 的 `model_tier_exact`（点名表），`tier=0` 清除。

    口径 ①（用户 2026-09-18 拍板）：手动指定后**以你为准** —— 能力分按该档顶值算，不再看 AA 榜分。
    """
    from app import capability as cap
    from app import config as cfgmod

    fake = {"channels": [], "aliases": {}, "route_strategy": "balanced", "pinned": [],
            "auth_enabled": False, "api_token": "", "port": 8787, "model_tier_exact": {}}
    saved = {}
    monkeypatch.setattr(cfgmod, "load_config", lambda: fake)
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.update(c))

    model = "Qwen/Qwen3.8-Flash-Next"
    auto = cap.tier_of(model)                       # 自动判定（没榜分就按家族启发式）

    r = client.post("/api/model-tier", json={"model": model, "tier": 3}, headers=H)
    j = r.json()
    assert r.status_code == 200, j
    assert j["manual"] is True and j["tier"] == 3
    assert j["tier_auto"] == auto                   # 界面要用它显示「自动判定：中档」
    assert fake["model_tier_exact"] == {model: 3}   # 落盘到点名表，不是正则表
    assert saved.get("model_tier_exact") == {model: 3}
    # 点名生效：档位按 3，能力分按该档顶值（口径 ①）
    assert cap.tier_of(model, cap.tier_overrides(fake)) == 3
    assert cap.capability_score(model, 3, cap.tier_overrides(fake)) == cap._OVERRIDE_ANCHOR[3]

    # 清除 → 键删掉，回到自动判定
    r = client.post("/api/model-tier", json={"model": model, "tier": 0}, headers=H)
    assert r.status_code == 200 and r.json()["manual"] is False
    assert model not in fake["model_tier_exact"]
    assert cap.tier_of(model, cap.tier_overrides(fake)) == auto

    # 非法值一律 400，不写配置
    for bad in (4, -1, "hi"):
        rr = client.post("/api/model-tier", json={"model": model, "tier": bad}, headers=H)
        assert rr.status_code == 400, bad
    assert client.post("/api/model-tier", json={"tier": 3}, headers=H).status_code == 400
    assert fake["model_tier_exact"] == {}


def test_settings_health_check_interval(client, monkeypatch):
    """设置页新增「健康检查间隔」：接受 1~1440，越界/非法值忽略（不写真实 config.json）"""
    from app import config as cfgmod
    fake = {"channels": [], "aliases": {}, "route_strategy": "balanced", "pinned": [],
            "auth_enabled": False, "api_token": "", "port": 8787, "check_interval_minutes": 30}
    saved = {}
    monkeypatch.setattr(cfgmod, "load_config", lambda: dict(fake))
    monkeypatch.setattr(cfgmod, "save_config", lambda c: (saved.clear(), saved.update(c)))

    r = client.post("/api/settings", json={"check_interval_minutes": 15}, headers=H)
    assert r.status_code == 200 and saved["check_interval_minutes"] == 15
    for bad in (0, -5, 5000, None, "abc"):
        saved.clear()
        client.post("/api/settings", json={"check_interval_minutes": bad}, headers=H)
        assert saved.get("check_interval_minutes", 30) == 30, bad


def test_models_test_no_channel(client):
    # 没有配置任何渠道 → 404
    r = client.post("/api/models/test", json={"model": "glm-4-flash"}, headers=H)
    assert r.status_code == 404


def test_models_test_no_candidate_keeps_previous_status(client, monkeypatch):
    """点「测试」但没有任何候选渠道时，**一个上游请求都没发** → 不许改模型状态。

    旧版在这里写一句笼统的「没有任何可用渠道（渠道未配置或健康检查未通过）」，后果两条：
      ① 把**原来更具体的原因**（403 / 余额不足 / 额度用完）冲掉；
      ② 没传 state → `mark_model_status` 默认按 `down` 记，一个 `limited`（等得到、会自愈）
         的模型被降级成 `down`（等不到、永不自愈）。
    2026-09-21 用户实测踩到：`deepseek-ai/DeepSeek-V4-Flash-0731` 的原因被冲成这句废话。"""
    from app import gateway
    from app import config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": [], "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    gateway.mark_model_status("ghost-model", False, "HTTP 403: 无权限", "HF", state="limited")
    try:
        r = client.post("/api/models/test", json={"model": "ghost-model"}, headers=H)
        assert r.status_code == 404, r.text
        st = gateway.model_status["ghost-model"]
        assert st["state"] == "limited", "没测过就不该把它降级成 down：" + repr(st)
        assert "403" in st["reason"], "原有的具体原因被冲掉了：" + repr(st)
        assert "403" in r.json()["detail"], "报错里也该带上已有原因：" + repr(r.json())
    finally:
        gateway.model_status.pop("ghost-model", None)


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


def test_overview_exposes_ratelimit_quota(client, monkeypatch):
    """官方额度要能被界面读到：渠道级 `quota_day`（魔搭账号级日剩余）+ 顶层
    `ratelimit`（模型级）与 `ratelimit_hits`（各口径被读到的累计次数）。

    没有这层透传，魔搭那 4 个响应头解析了也无处可看 —— 而
    `ratelimit_hits.modelscope` 长期为 0，正是区分「头没送上来」和「代码没解析」
    的唯一依据，别省掉它。"""
    from app import config as cfgmod
    from app import gateway, store
    # conftest 的函数级夹具把 DB 路径换成了本测试的临时目录，那边还没建表
    # （建表在 lifespan 里、走的是 module 级夹具的路径）→ 这里补一次
    store.init()
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "port": 8787, "channels": [{"id": "c_ms", "name": "魔搭 ModelScope",
                                    "type": "modelscope", "base_url": "http://x/v1",
                                    "api_key": "k", "enabled": True}],
        "aliases": {}, "route_strategy": "balanced", "pinned": []})
    gateway.user_quota["c_ms"] = {"remaining": 1873, "limit": 2000, "reset_ts": 9999999999}
    gateway.ratelimit_hits["modelscope"] = 5
    gateway.ratelimit[("m", "c_ms")] = {"remaining": 42, "reset_ts": 9999999999,
                                        "source": "modelscope"}
    try:
        d = client.get("/api/overview", headers=H).json()
        assert d["channels"][0]["quota_day"]["remaining"] == 1873
        assert d["channels"][0]["quota_day"]["limit"] == 2000
        assert d["ratelimit_hits"]["modelscope"] == 5
        assert d["ratelimit"]["m|c_ms"]["remaining"] == 42
    finally:                       # 全局状态，别串给别的测试
        gateway.user_quota.pop("c_ms", None)
        gateway.ratelimit.pop(("m", "c_ms"), None)
        gateway.ratelimit_hits["modelscope"] = 0
