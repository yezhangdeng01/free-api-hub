"""核心逻辑单元测试（离线，不打外部 API）"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import capability, gateway, store, vault  # noqa: E402


# ---------------- 测试夹具 ----------------
def _reset():
    gateway.channels.clear()
    gateway.cooldown.clear()
    gateway.stats.clear()
    gateway.channel_cool.clear()
    gateway.channel_down.clear()
    gateway.unverified.clear()


def _chan(cid, models, latency=100):
    """注册一个渠道的状态并返回其配置 dict"""
    cs = gateway.ChannelState()
    cs.models = models
    cs.latency_ms = latency
    cs.valid = True
    gateway.channels[cid] = cs
    return {"id": cid, "name": cid, "type": "custom", "base_url": "http://x/v1",
            "api_key": "k", "enabled": True}


def _cfg(chans, aliases=None, strategy="balanced"):
    return {"channels": chans, "aliases": aliases or {}, "route_strategy": strategy}


# ---------------- vault：DPAPI 加解密 ----------------
def test_vault_roundtrip():
    if os.name != "nt":
        pytest.skip("非 Windows 跳过")
    enc = vault.encrypt("sk-test-1234567890")
    assert enc.startswith("dpapi:")
    assert vault.decrypt(enc) == "sk-test-1234567890"
    assert vault.encrypt(enc) == enc  # 幂等
    assert vault.decrypt("plain") == "plain"
    assert vault.encrypt("") == ""
    assert vault.decrypt("dpapi:!!bad!!") == ""  # 坏密文 → 空串（安全降级）


# ---------------- capability：能力分层 ----------------
@pytest.mark.parametrize("mid,expect", [
    ("gpt-4o", 2), ("o3-mini", 1), ("claude-3-5-sonnet", 2),
    ("glm-4-flash", 1), ("gpt-4o-mini", 1), ("llama-3.1-8b-instruct", 1),
    ("glm-4-air", 1), ("some-unknown-model", 1), ("qwen2.5-72b", 2),
])
def test_tier_of(mid, expect):
    """不知名模型默认轻量（2026-09 起：不认识就保守压低，旗舰像的才给中档）"""
    assert capability.tier_of(mid) == expect


def test_tier_overrides():
    assert capability.tier_of("custom-xyz", {"xyz": 3}) == 3
    assert capability.tier_of("custom-xyz", {"bad:": 5}) == 1  # 非法覆盖忽略 → 落到「不知名=轻量」


def test_tier_scale_not_generation():
    """2026-09 修的那批「名不符实」：轻量只由规模决定，代际只降一档。"""
    # 前沿家族的「同代加速档」不再是轻量，deepseek 的 flash 更是同代主力 → 智能
    assert capability.tier_of("deepseek-ai/deepseek-v4-flash-0731") == 3
    assert capability.tier_of("deepseek-v4-flash") == 3
    # 无名家族：光凭 super/字面不给智能，只给中档；真·超大规模也给中档（不认识就保守）
    assert capability.tier_of("nvidia/nemotron-3-super-120b-a12b:free") == 2
    assert capability.tier_of("nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B") == 2
    assert capability.tier_of("nvidia/nemotron-3.5-lightning-30b-a3b") == 1  # 不知名且非旗舰像
    assert capability.tier_of("prism-ml/some-odd-27B") == 1
    # 同代 flash：gemini/glm 的 flash 是同代主力 → 智能；flash-lite 仍轻量
    assert capability.tier_of("models/gemini-3.6-flash") == 3
    assert capability.tier_of("models/gemini-3.5-flash-lite") == 1
    # 名字里写明 10~200B → 封顶中档（同代 ≠ 同尺寸）
    assert capability.tier_of("Qwen/Qwen3-14B") == 2
    assert capability.tier_of("Qwen/Qwen3.8-27B") == 2
    # 非对话模型（图片/视频/向量/翻译/安全）→ 轻量，不占智能档
    assert capability.tier_of("google/gemini-3-pro-image") == 1
    assert capability.tier_of("agnes-video-2.5-fast") == 1
    assert capability.tier_of("Qwen/Qwen3-Embedding-0.6B") == 1
    assert capability.tier_of("nvidia/riva-translate-4b-instruct-v2") == 1
    assert capability.tier_of("nvidia/llama-3.1-nemotron-safety-guard-8b-v3") == 1


def test_family_version_not_polluted_by_params():
    """家族版本号解析不能被参数量污染：Qwen-14B 不是「qwen 第 14 代」。"""
    from app import capability as cap
    cap._observed_frontier.clear()
    cap.update_frontier(["deepseek-ai/DeepSeek-R1-Distill-Qwen-14B", "Qwen/Qwen3.8-2.4T-A95B"])
    assert cap._observed_frontier["qwen"] == 3.8, cap._observed_frontier
    assert capability.tier_of("Qwen/Qwen3.8-2.4T-A95B") == 3          # 同代且够新 → 智能
    assert capability.tier_of("Qwen/Qwen3-235B-A22B") == 2           # 同代但早期次版本 → 中档
    cap._observed_frontier.clear()


def test_model_composite_takes_best_single_channel():
    """模型分必须取自**同一条渠道**：不能「稳定分来自 A + 延迟来自 B」拼出不存在的组合。"""
    m = {"tier": 3, "channels": [
        {"available": True, "score": 1.0, "latency_ms": 5000},   # 很稳但很慢
        {"available": True, "score": 0.1, "latency_ms": 100},    # 很快但很不稳
    ]}
    cap = gateway._CAP_BY_TIER[3]              # 手造 dict 没给 cap_score → 按档位锚点兜底
    got = gateway._model_composite(m, "speed")
    fast = 8 + 0.55 * cap + 0.45 * 0.1         # 快渠道：速度档 8 + 档内加权(智能5.5/稳定4.5)
    assert abs(got - fast) < 1e-9, got
    mixed = 8 + 0.55 * cap + 0.45 * 1.0        # 旧口径的「拼分」：快延迟 + 高稳定（不存在）
    assert got < mixed
    assert gateway._model_composite({"tier": 2, "channels": []}, "balanced") == 0.0


def test_capability_score_is_continuous(monkeypatch):
    """能力分是连续的（按 AA 榜分归一化），不再只有 3 档——「智能优先」才排得细"""
    from app import capability as cap
    monkeypatch.setattr(cap, "_bench", {"a": 42.3, "b": 7.8, "mid": 25.0})
    assert cap.capability_score("a") == 1.0
    assert cap.capability_score("b") == 0.0
    assert 0.4 < cap.capability_score("mid") < 0.6
    assert cap.capability_score("never-listed-model") == 0.10   # 无榜分 → 档位锚点（轻量）


def test_strategy_primary_with_tolerance_band(monkeypatch):
    """四种策略的口径：**主维度按容忍带宽优先，同档才看另两维**（用户 2026-09 定的）。

    能力带宽 0.10 ≈ AA 3.5 分，所以 AA 40 与 AA 39 视为「差不多」→ 稳定分说话；
    而 AA 45 高一档 → 稳定分再差也排在前面。"""
    from app import capability as cap
    _reset()
    monkeypatch.setattr(cap, "_bench", {"m-hi": 40.0, "m-lo": 39.0, "m-top": 45.0})
    ch = _chan("c1", [], latency=100)
    cfg = _cfg([ch])

    def setm(mid, stab, ttft):
        gateway.stats[(mid, "c1")] = {"score": stab, "latency": None, "ttft": ttft, "n": 9}

    c_hi = {"channel": ch, "model": "m-hi"}
    c_lo = {"channel": ch, "model": "m-lo"}
    c_top = {"channel": ch, "model": "m-top"}

    # 智能优先：同档内稳定分决定；跨档时能力压过稳定分
    setm("m-hi", 0.40, 5000)     # 能力略高但不稳
    setm("m-lo", 0.95, 5000)     # 能力略低但很稳（同一能力档）
    setm("m-top", 0.30, 9000)    # 能力高一档，最不稳最慢
    q_lo = gateway._composite(c_lo, cfg, "quality")
    q_hi = gateway._composite(c_hi, cfg, "quality")
    q_top = gateway._composite(c_top, cfg, "quality")
    assert q_lo > q_hi, (q_lo, q_hi)
    assert q_top > max(q_lo, q_hi), (q_top, q_lo, q_hi)

    # 稳定优先 / 速度优先：各自的主维度说话
    setm("m-hi", 0.95, 5000)     # 很稳但慢
    setm("m-lo", 0.20, 200)      # 不稳但很快
    assert gateway._composite(c_hi, cfg, "stability") > gateway._composite(c_lo, cfg, "stability")
    assert gateway._composite(c_lo, cfg, "speed") > gateway._composite(c_hi, cfg, "speed")

    # 均衡：三维直接加权，没有任何一个是「一票否决」的主维度
    b = gateway._composite(c_hi, cfg, "balanced")
    assert 0 < b < 1, b


def test_tier_from_bench_score(monkeypatch):
    """有权威榜单分（AA 智能指数）就用榜分定档，没有才回退命名启发式"""
    from app import capability as cap
    monkeypatch.setattr(cap, "_bench", {})
    cap.update_bench_scores({"deepseek-v4-flash-0731": 34.5,
                             "nemotron-3-super-120b-a12b": 13.6})
    assert cap.tier_of("deepseek-ai/deepseek-v4-flash-0731") == 3        # 34.5 → 智能
    assert cap.tier_of("nvidia/nemotron-3-super-120b-a12b:free") == 1    # 13.6 → 轻量
    # 跨渠道名归一化后能对上（厂商前缀 / :batch / 大小写都不影响）
    assert cap.bench_of("deepseek/deepseek-v4-flash-0731:batch") == 34.5
    assert cap.bench_of("models/gemini-9.9-flash") is None


def test_eff_score_confidence():
    """稳定分按样本量向先验收缩：测 1 次 ≠ 测很多次（① 的回归）"""
    assert gateway.eff_score({"score": 0.76, "n": 1}) < gateway.eff_score({"score": 0.99, "n": 20})
    # 侥幸成功 1 次几乎不改变判断（贴近先验 0.7）
    assert abs(gateway.eff_score({"score": 0.76, "n": 1}) - 0.7) < 0.02
    # 偶发 1 次连接失败不再直接打到谷底
    assert gateway.eff_score({"score": 0.35, "n": 1}) > 0.55
    # 连续失败照样沉底
    assert gateway.eff_score({"score": 0.02, "n": 10}) < 0.2
    # 老持久化数据没有 n → 按 6 个样本算，升级不归零
    assert gateway.eff_score({"score": 0.9}) > 0.8


def test_stats_persist_and_restore(tmp_path, monkeypatch):
    """渠道评分（稳定分/延迟/首字节/样本数）要跨重启保留——不然稳定分永远攒不起来"""
    from app import store as _store
    _reset()
    monkeypatch.setattr(_store, "RUNTIME_STATE_PATH", str(tmp_path / "rt.json"))
    gateway.mark_result("m1", "c1", True, 800)
    gateway.mark_result("m1", "c1", True, 900)
    gateway.mark_ttft("m1", "c1", 300)
    gateway.save_runtime_state()
    assert gateway.stats[("m1", "c1")]["n"] == 2
    gateway.stats.clear()
    gateway.restore_runtime_state()
    s = gateway.stats[("m1", "c1")]
    assert s["n"] == 2 and s["ttft"] == 300 and s["latency"] == 830  # 800→900 的 7:3 EMA


def test_ttft_wins_over_total_latency_in_speed_score():
    """速度分优先用首字节(TTFT)：总延迟一样时，首字节快的综合分应更高（② 的回归）"""
    _reset()
    ch = _chan("c1", ["m1", "m2"], latency=100)
    gateway.stats[("m1", "c1")] = {"score": 0.7, "latency": 3000, "ttft": 200, "n": 5}
    gateway.stats[("m2", "c1")] = {"score": 0.7, "latency": 3000, "ttft": 2500, "n": 5}
    cfg = _cfg([ch], strategy="speed")
    a = gateway._composite({"channel": ch, "model": "m1"}, cfg, "speed")
    b = gateway._composite({"channel": ch, "model": "m2"}, cfg, "speed")
    assert a > b, (a, b)


# ---------------- gateway：评分、冷却、别名、路由策略 ----------------
def test_model_view_latency_prefers_model_stat():
    """界面里的延迟 = 该模型在该渠道的实测延迟，没实测过才退回渠道健康检查延迟
    （与 _composite 同口径，否则界面顺序与真实选路对不上）。"""
    _reset()
    ch = _chan("c1", ["m1"], latency=1800)
    gateway.stats[("m1", "c1")] = {"score": 0.9, "latency": 250}
    entry = gateway.model_view(_cfg([ch]))[0]["channels"][0]
    assert entry["latency_ms"] == 250 and entry["score"] == 0.9
    gateway.stats.clear()
    assert gateway.model_view(_cfg([ch]))[0]["channels"][0]["latency_ms"] == 1800


def test_mark_result_cooldown_kinds():
    import time as _t
    _reset()
    gateway.mark_result("m", "c1", False, kind="rate_limit")
    assert gateway.cooldown[("m", "c1")] - _t.time() > 290
    _reset()
    gateway.mark_result("m", "c1", False, kind="server")
    assert gateway.cooldown[("m", "c1")] - _t.time() <= 70
    _reset()
    gateway.mark_result("m", "c1", False, kind="rate_limit", retry_after=600)
    assert gateway.cooldown[("m", "c1")] - _t.time() > 590
    _reset()
    gateway.mark_result("m", "c1", True)  # 成功解除冷却
    assert ("m", "c1") not in gateway.cooldown


def test_channel_quota_exhausted_cools_whole_channel(tmp_path, monkeypatch):
    """账户级「当天额度用完」：整个渠道冷却，所有模型都不进候选，且冷却持久化。"""
    import time as _t
    from app import store as _store
    _reset()
    monkeypatch.setattr(_store, "RUNTIME_STATE_PATH", str(tmp_path / "rt.json"))
    a = _chan("sc", ["m1", "m2"], latency=50)
    cfg = _cfg([a])
    # 触发账户级限额 → 整渠道冷却到明天
    gateway.mark_channel_quota_exhausted("sc", "当日额度用完")
    assert gateway.channel_cooling("sc")                      # 渠道在冷却
    assert gateway.candidates_for("m1", cfg) == []            # m1 不进候选
    assert gateway.candidates_for("m2", cfg) == []            # m2 也不进候选（全渠道被封）
    # 冷却持久化：save_runtime_state 已写入 channel_cool，restore 能读回
    gateway.restore_runtime_state()
    assert gateway.channel_cooling("sc")


def test_mark_result_scoring():
    _reset()
    gateway.mark_result("m", "c1", True, 100)
    gateway.mark_result("m", "c1", True, 100)
    gateway.mark_result("m", "c1", False)
    s = gateway.get_stat("m", "c1")
    assert 0 < s["score"] < 1
    assert s["score"] < 0.7  # 失败拉低评分
    assert s["latency"] == 100


def test_alias_expansion_in_candidates():
    _reset()
    a = _chan("a", ["real-a"], latency=200)
    b = _chan("b", ["real-b"], latency=50)
    cfg = _cfg([a, b], aliases={"virtual": ["real-a", "real-b"]})
    got = gateway.candidates_for("virtual", cfg)
    assert {c["model"] for c in got} == {"real-a", "real-b"}
    assert {c["channel"]["id"] for c in got} == {"a", "b"}


def test_candidates_speed_strategy():
    _reset()
    slow = _chan("slow", ["m"], latency=500)
    fast = _chan("fast", ["m"], latency=50)
    cfg = _cfg([slow, fast], strategy="speed")
    got = gateway.candidates_for("m", cfg)
    assert got[0]["channel"]["id"] == "fast"


def test_candidates_quality_prefers_high_tier():
    _reset()
    weak = _chan("weak", ["llama-3.1-8b-instruct"], latency=30)
    strong = _chan("strong", ["gpt-4o"], latency=500)
    cfg = _cfg([weak, strong], strategy="quality")
    got = gateway.candidates_for("gpt-4o", cfg)
    # 只有 strong 提供 gpt-4o，weak 渠道即使更快也不该被选（因为它没有该模型）
    assert {c["channel"]["id"] for c in got} == {"strong"}


# ---------------- store：记账与查询 ----------------
def test_store_log_and_query(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
    store.init()
    store.log_usage("c1", "ch1", "gpt-4o", 10, 20, 150, True)
    store.log_usage("c1", "ch1", "gpt-4o", 5, 5, 200, False, "boom")
    s = store.summary(days=1)
    assert s["totals"]["requests"] == 2
    assert s["totals"]["tokens"] == 40
    assert s["totals"]["errors"] == 1
    assert store.recent(limit=10)["logs"][0]["success"] == 0
    assert "gpt-4o" in store.distinct_models(days=1)


# ---------------- model_view：三态状态机（ok/limited/down） ----------------
def test_model_view_statuses():
    import time as _t
    _reset()
    gateway.channels.clear()
    # c1 提供 a（正常）与 b；c2 提供 c
    _chan("c1", ["model-a", "model-b"], latency=100)
    _chan("c2", ["model-c"], latency=300)
    cfg = _cfg([{"id": "c1", "name": "c1", "type": "custom", "base_url": "http://x/v1",
                 "api_key": "k", "enabled": True},
                {"id": "c2", "name": "c2", "type": "custom", "base_url": "http://x/v1",
                 "api_key": "k", "enabled": True}])
    # model-a：硬失败 → down
    gateway.mark_model_status("model-a", False, "HTTP 402", "c1", state="down")
    # model-b：429 限流 → limited（冷却中）
    gateway.mark_model_status("model-b", True, "限流", "c1", state="limited")
    gateway.cooldown[("model-b", "c1")] = _t.time() + 300
    # model-c：正常 → ok
    gateway.mark_model_status("model-c", True, "", "c2", state="ok")
    view = {m["id"]: m for m in gateway.model_view(cfg)}
    assert view["model-a"]["status"] == "down" and view["model-a"]["available"] is False
    assert view["model-b"]["status"] == "limited" and view["model-b"]["available"] is False
    assert view["model-c"]["status"] == "ok" and view["model-c"]["available"] is True


def test_model_status_available_semantics():
    """回归：limited（受限）绝不允许 available=True，否则「余额不足/限流」的模型会冒充可用。
    这是「重启后一大批不可用模型冒充可用，一用就报错」的病根。"""
    # limited → available 必须 False（只有 ok 才可路由）
    gateway.mark_model_status("m-limited", True, "余额不足，请充值", "c", state="limited")
    assert gateway.model_status["m-limited"]["available"] is False
    assert gateway.model_status["m-limited"]["state"] == "limited"
    # ok → available True
    gateway.mark_model_status("m-ok", True, "", "c", state="ok")
    assert gateway.model_status["m-ok"]["available"] is True
    # down → False
    gateway.mark_model_status("m-down", False, "HTTP 402", "c", state="down")
    assert gateway.model_status["m-down"]["available"] is False


def test_is_permanent_failure():
    from app.gateway import is_permanent_failure
    assert is_permanent_failure(402) is True
    assert is_permanent_failure(403) is True
    assert is_permanent_failure(404) is True   # batch 专用等，等也不会恢复
    assert is_permanent_failure(405) is True
    assert is_permanent_failure(429, "余额不足，请充值") is True
    assert is_permanent_failure(429, "Resource has been exhausted. Please recharge") is True
    assert is_permanent_failure(429, "rate limit exceeded per minute") is False
    assert is_permanent_failure(500) is False   # 5xx 暂时故障
    assert is_permanent_failure(503) is False


def test_restore_reclassifies_legacy_limited():
    """回归：旧数据里 limited 且 reason 含「余额/404」等永久失败，重启时应归正为 down。"""
    import time as _t
    now = _t.time()
    gateway.model_status.clear()
    # 模拟旧版脏数据：limited 但 available=True（历史 bug 写出的）
    gateway.model_status = {
        "glm-4.5": {"state": "limited", "available": True, "reason": "余额不足，请充值", "ts": now},
        "google/x:batch": {"state": "limited", "available": True, "reason": "HTTP 404: only available", "ts": now},
        "m-ok": {"state": "ok", "available": True, "reason": "", "ts": now},
    }
    # 直接调用归正逻辑（复刻 restore_model_status 内部）——通过 monkeypatch store 太绕，这里测纯函数化部分
    # 用 restore_model_status 但先 monkeypatch store.load_model_status
    from app import store
    store._ms_cache = None
    import json, tempfile, os
    old_path = store.MODEL_STATUS_PATH
    tmp = os.path.join(tempfile.gettempdir(), "ms_reclass.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gateway.model_status, f, ensure_ascii=False)
    store.MODEL_STATUS_PATH = tmp
    try:
        store._ms_cache = None
        gateway.model_status.clear()
        gateway.restore_model_status()
        assert gateway.model_status["glm-4.5"]["state"] == "down"
        assert gateway.model_status["glm-4.5"]["available"] is False
        assert gateway.model_status["google/x:batch"]["state"] == "down"
        assert gateway.model_status["m-ok"]["state"] == "ok"
        assert gateway.model_status["m-ok"]["available"] is True
    finally:
        store.MODEL_STATUS_PATH = old_path
        store._ms_cache = None
        os.remove(tmp)
    gateway.model_status.clear()


def test_reserved_auto_set():
    from app.gateway import RESERVED_AUTO, list_reserved_auto
    assert "auto" in RESERVED_AUTO and "auto:balanced" not in RESERVED_AUTO
    assert sorted(list_reserved_auto()) == ["auto", "auto:quality", "auto:speed", "auto:stability"]


# ---------------- throttle：429 自学水位 ----------------
def test_throttle_learn_and_block():
    import time as _t
    from app import throttle as th
    # 先清状态
    th._CALLS.clear(); th._LEARN.clear()
    cid, mid = "c1", "m1"
    # 未学习前不拦截
    assert th.blocked(cid, mid) is False
    # 模拟：打了 10 个调用，然后第 11 个撞 429（只观察一次，不启用）
    now = _t.time()
    for _ in range(10):
        th.record_call(cid, mid, now - 30)
    th.observe_429(cid, mid, now)
    assert th.blocked(cid, mid) is False  # 样本不足
    # 再过 30 分钟再次 429（第二次观测，启用）
    now2 = now + 1800
    for _ in range(12):
        th.record_call(cid, mid, now2 - 60)
    th.observe_429(cid, mid, now2)
    # 现在水位 = min(10,12) = 10 请求/分钟（观测点前窗口内）；当前 60 秒内有 12 次 → 应被拦
    assert th.blocked(cid, mid) is True


def test_throttle_forget_after_hour():
    import time as _t
    from app import throttle as th
    th._CALLS.clear(); th._LEARN.clear()
    cid, mid = "c2", "m2"
    now = _t.time()
    for _ in range(8):
        th.record_call(cid, mid, now - 10)
    th.observe_429(cid, mid, now)
    for _ in range(9):
        th.record_call(cid, mid, now - 5)
    th.observe_429(cid, mid, now)
    assert th.blocked(cid, mid) is True
    # 模拟距上次 429 已超 1 小时（把学习时间往前拨）→ 遗忘并放行
    th._LEARN[(cid, mid)]["learned"] -= th._FORGET_SEC + 1
    th._CALLS[(cid, mid)].clear()
    assert th.blocked(cid, mid) is False
    assert th.blocked(cid, mid) is False  # 学习值已清除，不再误伤



# ---------------- auto 路由必须尊重收藏（回归：收藏的模型排最前） ----------------
def test_auto_prefers_pinned_over_faster_unpinned():
    import time as _t
    _reset()
    gateway.model_status.clear()
    # 同档（都智能）: c1 慢的 deepseek（收藏）；c2 快的 qwen3.8（未收藏）
    # 未收藏时 quality 同分拼速度 → 应选更快的 qwen3.8
    _chan("slow", ["deepseek-v4-pro"], latency=2700)
    _chan("fast", ["qwen3.8-235b"], latency=300)
    chans = [_chan("slow", ["deepseek-v4-pro"], latency=2700),
             _chan("fast", ["qwen3.8-235b"], latency=300)]
    got = gateway.candidates_for_auto("quality", _cfg(chans, strategy="quality"))
    assert got[0]["model"] == "qwen3.8-235b", f"无收藏时应选快的，实际 {got[0]['model']}"
    # 收藏 deepseek：auto:quality 必须先试收藏的 deepseek（即使更慢）
    got2 = gateway.candidates_for_auto(
        "quality", {**_cfg(chans, strategy="quality"), "pinned": ["deepseek-v4-pro"]})
    assert got2[0]["model"] == "deepseek-v4-pro", \
        f"收藏的模型必须在候选最前，实际 {got2[0]['model']}"


# ---------------- 自适应前沿：看到新旗舰后旧代自动降档 ----------------
def test_frontier_auto_demote():
    from app import capability as cap
    cap._observed_frontier.clear()
    # 初始：静态门槛 gpt=5，给 gpt-4o → 中档；gpt-5 → 智能
    assert cap.tier_of("gpt-4o") == 2
    assert cap.tier_of("gpt-5") == 3
    # 渠道里出现了 gpt-6（下一任旗舰）
    cap.update_frontier(["openai/gpt-6", "openai/gpt-5", "openai/gpt-4o", "o5"])
    assert cap._observed_frontier["gpt"] >= 6
    # gpt-5 现在应自动降为中档；gpt-6 智能；mini 仍轻量
    assert cap.tier_of("gpt-5") == 2, "gpt-6 出现后 gpt-5 应自动降档"
    assert cap.tier_of("gpt-6") == 3
    assert cap.tier_of("gpt-6-mini") == 1
    cap._observed_frontier.clear()


def test_frontier_ignores_small_sku_and_family():
    from app import capability as cap
    cap._observed_frontier.clear()
    # 只有 mini/老代时不动门槛（mini 不算旗舰代际）
    cap.update_frontier(["gpt-4o-mini", "gpt-4o"])
    assert cap._observed_frontier.get("gpt", 0) == 4  # mini 被忽略，4o 是 4 代
    assert cap.tier_of("gpt-5") == 3  # 静态默认仍兜底
    cap._observed_frontier.clear()


# ---------------- 扫描状态跨重启持久化（修复：不可用/受限模型重启后回绿） ----------------
def test_throttle_snapshot_restore_roundtrip():
    import time as _t
    from app import throttle as th
    th._CALLS.clear(); th._LEARN.clear()
    cid, mid = "c1", "m1"
    now = _t.time()
    for _ in range(10):
        th.record_call(cid, mid, now - 30)
    th.observe_429(cid, mid, now)
    for _ in range(12):
        th.record_call(cid, mid, now + 1800 - 60)
    th.observe_429(cid, mid, now + 1800)  # 样本 ≥2 → 启用预判
    snap = th.snapshot()
    assert "c1|m1" in snap
    # 模拟重启：清内存后恢复
    th._CALLS.clear(); th._LEARN.clear()
    th.restore(snap)
    assert (cid, mid) in th.learned_pairs()


def test_model_name_migration():
    import time as _t
    from app.gateway import _migrate_named_models
    gateway.model_status.clear(); gateway.channel_down.clear()
    now = _t.time()
    gateway.model_status["Meta-Llama/Llama-3.3-70B-Instruct"] = {
        "available": False, "state": "down", "reason": "403", "ts": now, "channel": "OpenRouter"}
    gateway.channel_down[("Meta-Llama/Llama-3.3-70B-Instruct", "or1")] = {"reason": "x", "ts": now}
    old = ["Meta-Llama/Llama-3.3-70B-Instruct", "CohereLabs/aya-expanse-32b"]
    new = ["meta-llama/llama-3.3-70b-instruct", "cohere/command-a"]
    _migrate_named_models({"id": "or1", "type": "openrouter", "name": "OpenRouter"}, old, new)
    # 纯大小写改名 → 迁移
    assert "meta-llama/llama-3.3-70b-instruct" in gateway.model_status
    assert "Meta-Llama/Llama-3.3-70B-Instruct" not in gateway.model_status
    assert ("meta-llama/llama-3.3-70b-instruct", "or1") in gateway.channel_down
    # 模型本体不同（aya vs command-a）→ 不误迁
    assert "cohere/command-a" not in gateway.model_status
    gateway.model_status.clear(); gateway.channel_down.clear()
