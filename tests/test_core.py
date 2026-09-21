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
    gateway.channel_last_ok.clear()
    gateway.sticky.clear()          # 会话粘性：全局字典，必须逐测试清，否则会串
    gateway.ratelimit.clear()       # 官方限流头解析结果（模型级）
    gateway.user_quota.clear()      # 魔搭账号级日额度
    gateway.channel_quota_429.clear()   # 额度型 429 的「爆发度」窗口
    gateway.ratelimit_hits.update({"modelscope": 0, "x-ratelimit": 0})


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
        {"available": True, "stab": 1.0, "latency_ms": 5000},   # 很稳但很慢
        {"available": True, "stab": 0.1, "latency_ms": 100},    # 很快但很不稳
    ]}
    cap = gateway._CAP_BY_TIER[3]              # 手造 dict 没给 cap_score → 按档位锚点兜底
    # 速度档号从 speed_score 现算，别写死数字 —— 2026-09-20 速度维从双曲改成 log 线性化，
    # 同一个 100ms 的档号从 8 变成 10，写死就变成「改映射必挂」的假失败。
    band = int(gateway.speed_score(100) / 0.10)
    got = gateway._model_composite(m, "speed")
    fast = band + 0.55 * cap + 0.45 * 0.1      # 快渠道：速度档 + 档内加权(智能5.5/稳定4.5)
    assert abs(got - fast) < 1e-9, got
    mixed = band + 0.55 * cap + 0.45 * 1.0     # 旧口径的「拼分」：快延迟 + 高稳定（不存在）
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


def test_bench_cache_survives_restart(monkeypatch):
    """榜分/视觉能力落盘后重启可恢复（2026-09-15 修）——
    榜分只有 OpenRouter 一个来源，渠道一挂原本档位就退回名字启发式，评分等于没了。"""
    from app import capability as cap
    monkeypatch.setattr(cap, "_bench", {})
    monkeypatch.setattr(cap, "_vision_ok", set())
    monkeypatch.setattr(cap, "_vision_no", set())
    monkeypatch.setattr(cap, "_bench_last_ok", 0.0)
    monkeypatch.setattr(cap, "_dirty", {"v": False})

    cap.update_bench_scores({"glm-5.2": 60.0, "some-unknown-model": 60.0})
    cap.update_vision_models({"glm-5.2"}, {"cogview-4"})
    assert cap.cache_status()["dirty"] is True
    assert cap.save_cache() is True      # 脏了才写
    assert cap.save_cache() is False     # 干净就不写盘

    # 模拟进程重启：内存全清（此时若无缓存，无名模型只能落回「轻量」启发式）
    cap._bench.clear()
    cap._bench_last_ok = 0.0
    cap._vision_ok.clear()
    cap._vision_no.clear()
    assert cap.tier_of("some-unknown-model") == 1

    c = cap.load_cache()
    assert c["loaded"] is True and c["bench"] == 2
    assert cap.tier_of("some-unknown-model") == 3        # 榜分回来了 → 智能档
    assert cap.vision_known("glm-5.2") is True
    assert cap.vision_known("cogview-4") is False
    assert cap.cache_age_hours() is not None             # 新鲜度可用于判断要不要补数据
    assert cap.cache_status()["dirty"] is False          # 刚读回来，不必原样写回


def test_public_bench_source_keeps_cache_on_failure(monkeypatch):
    """兜底公开源够不着时**不能**把已有榜分清空——宁可分旧，不可分没了。"""
    import asyncio

    from app import capability as cap, providers

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("connect timeout")

    monkeypatch.setattr(cap, "_bench", {"keep-me": 40.0})
    info = asyncio.run(providers.fetch_bench_public(_Boom()))
    assert info["ok"] is False
    assert cap._bench == {"keep-me": 40.0}


def test_save_cache_never_overwrites_good_cache_with_empty(monkeypatch):
    """回归（2026-09-15 事故）：空内存**绝不能**写盘覆盖已有好缓存。

    当时 `tests/test_api.py` 的 module 级夹具漏了 `CAPABILITY_CACHE_PATH`，lifespan 退出时
    `save_cache(force=True)` 把空缓存写进生产文件 → 用户重启后全部模型「没分了」。"""
    from app import capability as cap, store

    monkeypatch.setattr(cap, "_bench", {})
    monkeypatch.setattr(cap, "_vision_ok", set())
    monkeypatch.setattr(cap, "_vision_no", set())
    monkeypatch.setattr(cap, "_bench_last_ok", 0.0)
    monkeypatch.setattr(cap, "_dirty", {"v": False})

    cap.update_bench_scores({"glm-5.3-flash": 41.9})
    assert cap.save_cache() is True
    with open(store.CAPABILITY_CACHE_PATH, encoding="utf-8") as f:
        good = f.read()
    assert "glm-5.3-flash" in good

    # 模拟测试进程收尾：内存被清空 + force 写
    cap._bench.clear()
    cap._dirty["v"] = True
    assert cap.save_cache(force=True) is False
    with open(store.CAPABILITY_CACHE_PATH, encoding="utf-8") as f:
        assert f.read() == good          # 盘上那份纹丝不动


@pytest.mark.parametrize("err,expect", [
    ("[Errno 11001] getaddrinfo failed", True),          # Windows DNS 解析失败（唤醒后典型）
    ("ConnectError: [Errno 11001] getaddrinfo failed", True),
    ("Temporary failure in name resolution", True),
    ("[Errno 101] Network is unreachable", True),        # 本机断网/网卡没起来
    ("connect: all connection attempts failed", True),
    ("HTTP 403: key 无效", False),                       # 上游的错，不是本机网络
    ("HTTP 402: 余额不足", False),
    ("HTTP 429 限流", False),
    ("", False),
])
def test_is_local_net_error(err, expect):
    """本机网络类失败识别（整轮全挂判定用）：DNS 解析失败是睡眠唤醒后的典型形状"""
    assert gateway.is_local_net_error(err) is expect


def _fake_channels(monkeypatch, m, errs: dict):
    """把 main 的渠道检查换成假实现：errs = {渠道id: 错误文本}（None=健康）"""
    chans = [{"id": cid, "name": cid, "enabled": True} for cid in errs]

    async def fake_check(client, ch):
        cs = m.gateway.ChannelState()
        e = errs[ch["id"]]
        if e:
            cs.valid, cs.error = False, e
        else:
            cs.valid, cs.error, cs.models = True, None, ["some-model"]
        return cs

    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"channels": chans})
    monkeypatch.setattr(m.gateway, "sync_channels", lambda cfg: None)
    monkeypatch.setattr(m.gateway, "refresh_channel", fake_check)

    async def no_topup(cfg):
        return None

    monkeypatch.setattr(m, "_topup_bench_cache", no_topup)
    monkeypatch.setattr(m, "_bg", {"last_check": 0.0, "last_quota": 0.0, "last_bench": 0.0,
                                   "net_down": 0, "next_check": 0.0})


def test_refresh_all_backs_off_on_local_net_down(monkeypatch):
    """回归（2026-09-16 事故）：睡眠唤醒后整轮 DNS 失败 → 短退避重试，不等一整个检查周期。

    当时七个渠道一起 `getaddrinfo failed`，网络 15 分钟后就恢复了，但健康检查要等下一个
    `check_interval_minutes`（默认 30 分钟）才再来 —— 用户看到「所有渠道失败、没有模型可用，
    必须重启服务或手动测试」。"""
    import asyncio
    import time

    from app import main as m

    _reset()
    _fake_channels(monkeypatch, m, {"c1": "[Errno 11001] getaddrinfo failed",
                                    "c2": "getaddrinfo failed"})
    asyncio.run(m.refresh_all())
    assert m._bg["net_down"] == 1
    assert m._bg["next_check"] > time.time()          # 已排下一次重试
    delay_1 = m._bg["next_check"] - time.time()
    assert 8 <= delay_1 <= 11                          # 第一次 10s

    asyncio.run(m.refresh_all())                       # 还挂着 → 退避加长
    assert m._bg["net_down"] == 2
    assert m._bg["next_check"] - time.time() > delay_1

    # 有一个渠道恢复 → 立刻回到正常排期，退避清零
    _fake_channels(monkeypatch, m, {"c1": "getaddrinfo failed", "c2": None})
    asyncio.run(m.refresh_all())
    assert m._bg["net_down"] == 0 and m._bg["next_check"] == 0.0


def test_refresh_all_does_not_back_off_on_real_upstream_failure(monkeypatch):
    """整轮全挂但**不是**本机网络问题（403/额度）→ 不许走快重试，否则会变成重试风暴。"""
    import asyncio

    from app import main as m

    _reset()
    _fake_channels(monkeypatch, m, {"c1": "HTTP 403: key 无效", "c2": "HTTP 402: 余额不足"})
    asyncio.run(m.refresh_all())
    assert m._bg["net_down"] == 0
    assert m._bg["next_check"] == 0.0


def test_strategy_primary_with_tolerance_band(monkeypatch):
    """四种策略的口径：**主维度按容忍带宽优先，同档才看另两维**（用户 2026-09 定的）。

    能力带宽 0.10 ≈ AA 3.5 分，所以 AA 40 与 AA 39 视为「差不多」→ 稳定分说话；
    而 AA 45 高一档 → 稳定分再差也排在前面。"""
    from app import capability as cap
    _reset()
    monkeypatch.setattr(cap, "_bench", {"m-hi": 40.0, "m-lo": 39.0, "m-top": 45.0})
    ch = _chan("c1", [], latency=100)
    cfg = _cfg([ch])

    def setm(mid, wins, ttft):
        """wins = 最近 10 次真实调用里的成功次数 → 稳定分 = 0.7 + (wins/10 - 0.7) × 10/12"""
        gateway.stats[(mid, "c1")] = {"win": [1] * wins + [0] * (10 - wins),
                                      "latency": None, "ttft": ttft}

    c_hi = {"channel": ch, "model": "m-hi"}
    c_lo = {"channel": ch, "model": "m-lo"}
    c_top = {"channel": ch, "model": "m-top"}

    # 智能优先：同档内稳定分决定；跨档时能力压过稳定分
    setm("m-hi", 4, 5000)        # 能力略高但不稳（4/10）
    setm("m-lo", 10, 5000)       # 能力略低但很稳（10/10，同一能力档）
    setm("m-top", 3, 9000)       # 能力高一档，最不稳最慢
    q_lo = gateway._composite(c_lo, cfg, "quality")
    q_hi = gateway._composite(c_hi, cfg, "quality")
    q_top = gateway._composite(c_top, cfg, "quality")
    assert q_lo > q_hi, (q_lo, q_hi)
    assert q_top > max(q_lo, q_hi), (q_top, q_lo, q_hi)

    # 稳定优先 / 速度优先：各自的主维度说话
    setm("m-hi", 10, 5000)       # 很稳但慢
    setm("m-lo", 2, 200)         # 不稳但很快
    assert gateway._composite(c_hi, cfg, "stability") > gateway._composite(c_lo, cfg, "stability")
    assert gateway._composite(c_lo, cfg, "speed") > gateway._composite(c_hi, cfg, "speed")

    # 均衡：三维直接加权，没有任何一个是「一票否决」的主维度
    b = gateway._composite(c_hi, cfg, "balanced")
    assert 0 < b < 1, b


def test_vision_from_platform_data(monkeypatch):
    """视觉能力：平台数据（OpenRouter architecture.input_modalities）优先于名字启发式"""
    from app import capability as cap
    monkeypatch.setattr(cap, "_vision_ok", set())
    monkeypatch.setattr(cap, "_vision_no", set())
    cap.update_vision_models({"qwen/qwen3.5-9b"} | {cap.norm_id(x) for x in ("Qwen/Qwen3.5-9B",)},
                             {cap.norm_id(x) for x in ("gemini-2.5-flash-preview-tts",)})
    # 名字里没有 vl/vision，但平台说能看图 → 标视觉（Qwen3.5-9B 这类，实测漏标 250+ 个）
    assert cap.meta_of("Qwen/Qwen3.5-9B")["vision"] is True
    # 名字像视觉（gemini-*）但平台明确说不能 → 纠正为不标
    assert cap.meta_of("gemini-2.5-flash-preview-tts")["vision"] is False
    # 没数据才回退名字启发式；且 TTS/音频/绘图类不再误标
    assert cap.meta_of("some-vl-model")["vision"] is True
    assert cap.meta_of("plain-text-model")["vision"] is False
    assert cap.meta_of("gemini-3-pro-image")["vision"] is False


def test_auto_vision_filters_and_orders(monkeypatch):
    """auto-vision：候选**只含能看图**的模型，顺序 = 视觉策略（能力优先，同档看稳定/速度）"""
    from app import capability as cap
    _reset()
    monkeypatch.setattr(cap, "_bench", {"vl-strong": 40.0, "vl-weak": 20.0})
    ch = _chan("c1", ["vl-strong", "vl-weak", "text-only"], latency=100)
    cfg = _cfg([ch])
    for m, ttft in (("vl-strong", 3000), ("vl-weak", 300), ("text-only", 100)):
        gateway.stats[(m, "c1")] = {"score": 0.7, "latency": None, "ttft": ttft, "n": 5}
    got = [c["model"] for c in gateway.candidates_for_auto("vision", cfg)]
    # text-only 被滤掉；能力档高的在前（虽然它更慢）——视觉优先是「能力优先」
    assert got == ["vl-strong", "vl-weak"], got
    # 非视觉策略不受影响
    assert "text-only" in [c["model"] for c in gateway.candidates_for_auto("quality", cfg)]


def test_tier_override_wins_over_bench(monkeypatch):
    """用户显式 model_tiers 覆盖优先于榜分（否则覆盖了档位、排序却不跟着变）"""
    from app import capability as cap
    monkeypatch.setattr(cap, "_bench", {"some-weak-vlm": 12.0})
    ov = {"some-weak-vlm": 3}
    assert cap.tier_of("some-weak-vlm", ov) == 3
    assert cap.capability_score("some-weak-vlm", 3, ov) == 1.0        # 覆盖 → 该档顶值
    assert cap.capability_score("some-weak-vlm", 1, None) < 0.5       # 不覆盖 → 按榜分（12 → 0.12）


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


def test_stab_window_score():
    """稳定分 = 近 N 次**真实调用**成功率（向先验 0.7 收缩）；没数据 → None（不参与加权）"""
    from app.gateway import stab_of, stab_raw
    assert stab_of({"win": []}) is None and stab_of({}) is None
    assert stab_raw({"win": []}) is None
    assert abs(stab_of({"win": [1, 1, 1]}) - 0.88) < 1e-6      # 0.7 + 0.3×(3/5)
    assert abs(stab_of({"win": [0, 0, 0]}) - 0.28) < 1e-6      # 0.7 - 0.7×(3/5)
    assert stab_of({"win": [1] * 10}) > stab_of({"win": [1] * 2}) > 0.7   # 样本越多越接近原始率
    assert stab_of({"win": [1, 0] * 5}) < 0.71 and stab_raw({"win": [1, 0] * 5}) == 0.5


def test_stab_only_real_calls_and_technical_failures(monkeypatch):
    """稳定分只吃真实调用；且只认技术性失败——429/余额/4xx/本地网络一律不进窗口"""
    from app import store
    monkeypatch.setattr(store, "persist_runtime_state", lambda *a, **k: None)
    gateway.stats.clear()
    gateway.mark_result("m1", "c1", True, 100)                                  # 默认 probe → 不记
    assert gateway.get_stat("m1", "c1")["win"] == []
    gateway.mark_result("m1", "c1", True, 100, source="real")
    gateway.mark_result("m1", "c1", False, kind="rate_limit", source="real")    # 429 限流 → 不进
    gateway.mark_result("m1", "c1", False, kind="balance", source="real")       # 余额不足 → 不进
    gateway.mark_result("m1", "c1", False, kind="client", source="real")        # 4xx 权限 → 不进
    gateway.mark_result("m1", "c1", False, kind="local_net", source="real")     # 本地 DNS → 不进
    assert gateway.get_stat("m1", "c1")["win"] == [1]
    for k in ("stream_break", "connect", "timeout", "server", "bad_json"):      # 技术性失败 → 进
        gateway.mark_result("m1", "c1", False, kind=k, source="real")
    assert gateway.get_stat("m1", "c1")["win"] == [1, 0, 0, 0, 0, 0]
    gateway.mark_result("m1", "c1", False, source="real")                       # 未知失败 → 也计
    assert gateway.get_stat("m1", "c1")["win"] == [1, 0, 0, 0, 0, 0, 0]
    for _ in range(12):                                                          # 窗口只留最近 10 次
        gateway.mark_result("m1", "c1", True, source="real")
    assert len(gateway.get_stat("m1", "c1")["win"]) == 10
    gateway.stats.clear()


def test_score_dims_without_stab_data():
    """均衡（加权）：没有真实样本 → 稳定维剔除、权重归一给 cap/spd（与前端 compScore 同口径）"""
    from app.gateway import _score_dims
    w = (0.35, 0.40, 0.25)
    assert abs(_score_dims((1.0, None, 0.0), "balanced")
               - (w[0] * 1.0 + w[2] * 0.0) / (w[0] + w[2])) < 1e-9
    # 有数据时走原加权公式
    assert abs(_score_dims((1.0, 0.5, 0.0), "balanced")
               - (w[0] + w[1] * 0.5)) < 1e-9
    # 主维度=稳定的策略遇到 None：按先验 0.7 当中性，不抛错、仍能排序
    assert _score_dims((0.9, None, 0.9), "stability") == _score_dims((0.9, 0.7, 0.9), "stability")


def test_classify_429_three_tiers():
    """429 四档：余额不足（要充值）/ 免费额度用尽（等明天）/ 每分钟限流 / 未知

    关键用例来自实盘：**魔搭的每日免费额度用完，正文就是 insufficient balance**，
    与 OpenRouter 真欠费的正文完全一样，只能靠渠道类型区分。"""
    from app.gateway import classify_429
    bal = '{"error":{"message":"insufficient balance"}}'
    label, secs, _ = classify_429(bal, "modelscope")          # 魔搭免费日额度
    # 冷却到明天：本地 23:5x 时离次日 00:05 已不足 10 分钟 → 被下限夹成 600，
    # 所以下界用 `>=`（原来写 `> 600`，每天最后 10 分钟必挂一次）
    assert label == "free_daily" and 600 <= secs <= 86400
    label, secs, _ = classify_429(bal, "openrouter")          # 真·欠费 → 不冷却，按 down
    assert label == "paid_balance" and secs == 0
    label, _, _ = classify_429('{"error":{"message":"You exceeded your current quota, '
                               'please check your plan and billing details"}}', "gemini")
    assert label == "free_daily"                              # 免费渠道的 quota/billing 措辞
    label, secs, _ = classify_429('{"status":429,"title":"Too Many Requests"}', "nim")
    assert label == "minute" and secs == 90
    label, secs, _ = classify_429('{"error":{"message":"daily quota"}}', "modelscope", retry_after=60)
    assert label == "minute"                                  # 上游说 60s 能好 → 不按明天冷却
    label, secs, _ = classify_429("something odd", "custom")
    assert label == "unknown" and secs == 300


def test_vision_verified_and_heuristic():
    """视觉判定：实测表 > 平台数据 > 名字启发式；产出型/转写类必须排除"""
    from app.capability import meta_of
    assert meta_of("agnes-3.0-flash")["vision"] is True            # 真图实测（OpenRouter 无此模型）
    assert meta_of("agnes-2.5-pro")["vision"] is True
    assert meta_of("agnes-image-2.5-flash")["vision"] is False     # 产出型，不收图
    assert meta_of("agnes-video-2.5")["vision"] is False
    assert meta_of("models/gemini-3.5-transcribe")["vision"] is False   # 实测 400
    assert meta_of("models/gemini-3.5-live-translate-preview")["vision"] is False  # Live 流式翻译，不收图
    assert meta_of("models/gemini-3.7-flash")["vision"] is True
    assert meta_of("inclusionai/ling-3.0-flash-vl:free")["vision"] is True


def test_stats_persist_and_restore(tmp_path, monkeypatch):
    """渠道评分（稳定分/延迟/首字节/样本数）要跨重启保留——不然稳定分永远攒不起来"""
    from app import store as _store
    _reset()
    monkeypatch.setattr(_store, "RUNTIME_STATE_PATH", str(tmp_path / "rt.json"))
    gateway.mark_result("m1", "c1", True, 800, source="real")
    gateway.mark_result("m1", "c1", True, 900, source="real")
    gateway.mark_ttft("m1", "c1", 300)
    gateway.save_runtime_state()
    assert gateway.stats[("m1", "c1")]["win"] == [1, 1]
    gateway.stats.clear()
    gateway.restore_runtime_state()
    s = gateway.stats[("m1", "c1")]
    assert s["win"] == [1, 1] and s["ttft"] == 300 and s["latency"] == 830  # 800→900 的 7:3 EMA


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
    gateway.stats[("m1", "c1")] = {"win": [1] * 10, "latency": 250}
    entry = gateway.model_view(_cfg([ch]))[0]["channels"][0]
    assert entry["latency_ms"] == 250 and entry["score"] == 1.0 and entry["samples"] == 10
    assert entry["stab"] >= 0.94        # 10/10 成功 → 0.7 + 0.3×(10/12) = 0.95
    gateway.stats.clear()
    assert gateway.model_view(_cfg([ch]))[0]["channels"][0]["latency_ms"] == 1800


def test_model_view_channel_exposes_type_for_search():
    """渠道条目必须带 `channel_type`（前端搜索按平台筛：provider:nim / provider:modelscope）。

    只靠 `channel_name` 做匹配的话，用户在设置里把渠道名改一下（「魔搭 ModelScope」→「小魔搭」），
    按平台搜索就再也搜不到了（2026-09-21 加的字段）。"""
    _reset()
    ch = _chan("c1", ["m1"], latency=500)
    entry = gateway.model_view(_cfg([ch]))[0]["channels"][0]
    assert entry["channel_type"] == "custom", entry
    assert entry["channel_name"] == "c1", entry


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


def test_candidates_for_test_bypasses_channel_cool():
    """手动测试候选：渠道级冷却中的渠道也给出（用户主动测试）；**硬失败也给出**。

    2026-09-21 起 `channel_down` 不再排除（用户拍板的方案 B）：`down` 没有过期时间，
    只靠「一次成功调用」清除，而自动路由和本函数以前都把它挡住 → 误判成 down 的模型
    再也回不来（只能去渠道卡片「扫描全部模型」，还连带测整条渠道）。手动测试穿透它：
    错的能救回，真死的点几次还是同一句错。
    """
    _reset()
    a = _chan("c1", ["m1"], latency=50)
    b = _chan("c2", ["m1"], latency=80)
    cfg = _cfg([a, b])
    gateway.mark_channel_quota_exhausted("c1", "当日额度用完")
    # 正常候选：c1（渠道级冷却）被过滤，只剩 c2
    assert [c["channel"]["id"] for c in gateway.candidates_for("m1", cfg)] == ["c2"]
    # 手动测试候选：冷却中的 c1 也在（顺序按策略分，正常渠道不一定排前）
    got = gateway.candidates_for_test("m1", cfg)
    assert {c["channel"]["id"] for c in got} == {"c1", "c2"}
    # 硬失败渠道**也**给候选：这是 down 唯一的出口，不给就永远救不回来
    gateway.mark_channel_down("m1", "c2", "HTTP 403")
    got = gateway.candidates_for_test("m1", cfg)
    assert {c["channel"]["id"] for c in got} == {"c1", "c2"}
    # 但「官方限流头说额度耗尽且未到重置时刻」仍排除：那是**自愈**的时间窗封锁，
    # 到点自己会解，点了也还是失败（别和 down 混淆）
    gateway.note_ratelimit_headers("c2", "m1", _Headers({
        "Modelscope-Ratelimit-Model-Requests-Limit": "500",
        "Modelscope-Ratelimit-Model-Requests-Remaining": "0",
    }))
    assert gateway.ratelimit_exhausted("m1", "c2") is True
    got = gateway.candidates_for_test("m1", cfg)
    assert [c["channel"]["id"] for c in got] == ["c1"]


def test_mark_result_ok_clears_channel_cool(tmp_path, monkeypatch):
    """手动测试成功 → 解除渠道级冷却（额度已恢复），且落盘后重启不复活。"""
    from app import store as _store
    _reset()
    monkeypatch.setattr(_store, "RUNTIME_STATE_PATH", str(tmp_path / "rt.json"))
    a = _chan("sc", ["m1"], latency=50)
    cfg = _cfg([a])
    gateway.mark_channel_quota_exhausted("sc", "当日额度用完")
    assert gateway.channel_cooling("sc")
    assert gateway.candidates_for_test("m1", cfg)            # 冷却中但手动测试仍可拿到候选
    gateway.mark_result("m1", "sc", True)                    # 手动测试成功
    assert not gateway.channel_cooling("sc")                 # 渠道冷却被解除
    assert gateway.candidates_for("m1", cfg) != []           # 正常候选恢复
    gateway.restore_runtime_state()                          # 重启模拟：不复活
    assert not gateway.channel_cooling("sc")


def test_quota_429_model_level_then_account_level():
    """额度型 429 的层级判定：窗口内 <4 个模型 → **模型级**（只冷该模型，渠道照常路由）；
    第 4 个不同模型 → **账号级**（整渠道停到明天）。

    旧行为是「一见额度 429 就整渠道停到明天」：魔搭大模型日额度只有 100，先耗完的那个撞
    一次就把整条渠道封十几个小时。近三周 62 次魔搭 429 实测里至少 4 天是这种误伤
    （当天渠道其实还在正常服务，有成功请求为证）。"""
    import time as _t
    _reset()
    cfg = _cfg([_chan("ms", [f"m{i}" for i in range(1, 6)])])
    for i in (1, 2, 3):
        acc, n = gateway.note_channel_quota_429("ms", f"m{i}")
        assert acc is False and n == i
        assert not gateway.channel_cooling("ms")          # 渠道没被封
    assert gateway.quota_429_models("ms") == 3
    acc, n = gateway.note_channel_quota_429("ms", "m3")   # 同一个模型再撞：不算新模型
    assert acc is False and n == 3
    acc, n = gateway.note_channel_quota_429("ms", "m4")   # 第 4 个不同模型 → 账号级
    assert acc is True and n == 4
    assert gateway.channel_cooling("ms")
    # 下界取 599：临近午夜时 `_secs_to_tomorrow` 会被 10 分钟下限夹住（见上一测试）
    assert 599 <= gateway.channel_cool["ms"] - _t.time() <= 86400
    assert gateway.candidates_for("m5", cfg) == []        # 整渠道都不进候选


def test_quota_429_uses_burst_window_not_daily_tally():
    """判据是**短窗口内的爆发度**，不是「当天累计」：4 个模型分散在一天里各撞一次
    （大模型日额度小、慢慢耗完）不该被误判成账号级 —— 那等于把误伤换了个形式。"""
    import time as _t
    _reset()
    t0 = _t.time()
    for i in (1, 2, 3):
        acc, _ = gateway.note_channel_quota_429("ms", f"m{i}", now=t0)
        assert acc is False
    assert gateway.quota_429_models("ms", now=t0) == 3
    acc, n = gateway.note_channel_quota_429("ms", "m4", now=t0 + 1860)   # 31 分钟后
    assert acc is False and n == 1                        # 老的三个已滑出窗口
    assert not gateway.channel_cooling("ms")


def test_speed_score_is_log_linear():
    """速度分 = log 线性化（100ms→1.0，60s→0.0）。"""
    ss = gateway.speed_score
    assert ss(0) == 1.0 and ss(50) == 1.0 and ss(100) == 1.0
    assert ss(60000) == 0.0 and ss(120000) == 0.0
    assert ss(None) == 0.0 and ss("bad") == 0.0
    assert ss(100) > ss(400) > ss(2000) > ss(10000) > ss(55000)
    # 关键回归：3.5s 与 38.8s 在旧双曲映射下同桶 0（速度失效），现在必须分档
    assert int(ss(3500) / 0.10) > int(ss(38800) / 0.10)


def test_speed_strategy_orders_slow_models_by_latency():
    """速度优先视图：慢模型之间也要按延迟分出先后（用户 2026-09-20 报的 bug）。

    旧双曲映射下「4.0s 和 38.8s」都落在桶 0，桶内只看能力/稳定分 →
    一个 38.8s 的模型能凭稳定分排在 4.0s 的前面。"""
    _reset()
    ch = _chan("c1", ["fast", "slow"], latency=100)
    gateway.stats[("fast", "c1")] = {"score": 0.7, "latency": 4000, "ttft": 4000, "win": []}
    gateway.stats[("slow", "c1")] = {"score": 1.0, "latency": 38800, "ttft": 38800,
                                     "n": 9, "win": [1] * 9}
    cfg = _cfg([ch], strategy="speed")
    a = gateway._composite({"channel": ch, "model": "fast"}, cfg, "speed")
    b = gateway._composite({"channel": ch, "model": "slow"}, cfg, "speed")
    assert a > b, (a, b)   # 就算 slow 的稳定分更高，4.0s 也该排在 38.8s 前面


def test_mark_result_scoring():
    """真实调用的结果进稳定分窗口；探测（默认 source）只碰可用性、不进窗口"""
    _reset()
    gateway.mark_result("m", "c1", True, 100, source="real")
    gateway.mark_result("m", "c1", True, 100, source="real")
    gateway.mark_result("m", "c1", False, source="real")
    s = gateway.get_stat("m", "c1")
    assert s["win"] == [1, 1, 0] and gateway.stab_raw(s) == 0.667
    assert gateway.stab_of(s) < 0.7      # 失败拉低稳定分
    assert s["latency"] == 100
    gateway.mark_result("m", "c1", False, kind="connect")     # 默认 = probe
    assert gateway.get_stat("m", "c1")["win"] == [1, 1, 0]


def test_geo_block_not_model_fault():
    """地域封锁 = 本机出口网络问题：不算永久失败（不标 down）、不进稳定分"""
    from app.gateway import is_geo_block, is_permanent_failure, kind_from_error, _STAB_SKIP_KINDS
    geo = ('{"error":{"code":400,"message":"User location is not supported for the API use.",'
           '"status":"FAILED_PRECONDITION"}}')
    assert is_geo_block(geo)
    assert not is_permanent_failure(400, geo)            # 400 但地域封锁 → 不记 down
    assert kind_from_error(geo) == "geo" and "geo" in _STAB_SKIP_KINDS
    assert not is_geo_block('{"error":{"message":"model is not available"}}')
    assert is_permanent_failure(400, '{"error":{"message":"model not found"}}')


def test_backfill_stab_from_usage_rows(monkeypatch, tmp_path):
    """历史真实调用回填窗口：429/余额/4xx/本地网络/地域 跳过；已有实时样本的不覆盖"""
    from app import store as _store
    _reset()
    monkeypatch.setattr(_store, "RUNTIME_STATE_PATH", str(tmp_path / "rt.json"))
    gateway.stats.clear()
    rows = [                                                            # (模型,渠道,成功,error,ts) 新→旧
        ("m1", "c1", 1, "", 300.0),                                     # 成功 → 1
        ("m1", "c1", 0, "connect: Server disconnected", 200.0),          # 技术性失败 → 0
        ("m1", "c1", 0, "客户端断开", 150.0),                             # 客户端中断（老数据无列，靠文本兜底）→ 跳过
        ("m1", "c1", 0, "客户端断开", 130.0, 1),                           # 客户端中断（带 cancelled 列）→ 跳过
        ("m1", "c1", 0, "HTTP 429 当日额度用完", 100.0),                  # 429 → 跳过
        ("m1", "c1", 0, "connect: [Errno 11001] getaddrinfo failed", 50.0),  # 本地 DNS → 跳过
        ("m2", "c1", 0, 'HTTP 400: {"error":"User location is not supported"}', 40.0),  # 地域 → 跳过
        ("m3", "c1", 1, "", 30.0),
    ]
    assert gateway.backfill_stab(rows) == 2
    assert gateway.stats[("m1", "c1")]["win"] == [0, 1]      # 存成旧的在前（旧=失败,新=成功）
    assert gateway.stats[("m3", "c1")]["win"] == [1]
    assert ("m2", "c1") not in gateway.stats                 # 整组都是跳过的失败 → 不建条目
    gateway.stats[("m3", "c1")]["win"] = [0, 0]              # 已有实时样本 → 不覆盖
    gateway.backfill_stab(rows)
    assert gateway.stats[("m3", "c1")]["win"] == [0, 0]
    gateway.stats.clear()


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


def test_cancelled_client_abort_not_counted(tmp_path, monkeypatch):
    """客户端主动断开（用户点停止 / 客户端超时）上游其实正常：不计失败、不进稳定分。

    背景：魔搭推理模型响应慢，客户端等不及先断，旧版把这些记成 success=0，
    统计页显示一堆假失败（某天魔搭 80 条）。"""
    from app.gateway import kind_from_error, _STAB_SKIP_KINDS
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t_cancel.db"))
    store.init()
    store.log_usage("c1", "ch1", "m", 10, 0, 4000, False, "客户端断开",
                    upstream_model="m", cancelled=True)
    store.log_usage("c1", "ch1", "m", 10, 20, 3000, True, upstream_model="m")
    s = store.summary(days=1)
    assert s["totals"]["requests"] == 2      # 中断也是真实发出的请求，计数保留
    assert s["totals"]["errors"] == 0        # 但不算失败
    assert s["totals"]["cancelled"] == 1
    assert s["totals"]["tokens"] == 40
    logs = store.recent(limit=5)["logs"]
    assert logs[0]["success"] == 1 and logs[0]["cancelled"] == 0
    assert logs[1]["success"] == 0 and logs[1]["cancelled"] == 1
    # 回填口径：中断不是模型不稳
    assert kind_from_error("客户端断开") == "cancelled"
    assert kind_from_error("客户端中断（无结束标记）") == "cancelled"
    assert "cancelled" in _STAB_SKIP_KINDS


def test_tier_overrides_exact_beats_regex_and_is_escaped():
    """手动档位两张表：界面点出来的「精确表」优先，且模型 id 按**原文**匹配。

    为什么精确表不能塞进正则表：模型 id 里的 `.` 是正则通配符（`Qwen3.8` 会连 `Qwen3X8` 一起匹配），
    带 `(` `+` 的 id 更会直接 `re.error` 被静默跳过 —— 那正是「改了没生效」的经典坑。
    而配置里手写的 `model_tiers` 本来就是正则（批量规则），语义不同，所以分两张表、精确优先。
    """
    from app import capability as cap

    cfg = {"model_tier_exact": {"Qwen/Qwen3.8-Flash-Next": 3, "x(1)/y": 1},
           "model_tiers": {".*flash": 2}}
    ov = cap.tier_overrides(cfg)

    # 点名赢过批量规则（两张表都命中 → 精确表的 3）
    assert cap.override_tier("Qwen/Qwen3.8-Flash-Next", ov) == 3
    assert cap.tier_of("Qwen/Qwen3.8-Flash-Next", ov) == 3
    # 只被正则命中的照旧走正则表
    assert cap.override_tier("ZhipuAI/GLM-5.3-Flash", ov) == 2
    # `.` 不当通配符用：Qwen3X8 不该被点名，只能靠正则表拿到 2
    assert cap.override_tier("Qwen/Qwen3X8-Flash-Next", ov) == 2
    # 括号同理：`x(1)/y` 不能把「x1/y」也匹配上（未转义就会误伤）
    assert cap.override_tier("x(1)/y", ov) == 1
    assert cap.override_tier("x1/y", ov) is None
    # 非法值忽略，不炸也不写坏表
    assert cap.tier_overrides({"model_tier_exact": {"m1": 9, "m2": "abc", "": 3}}) == {}
    # 没有任何手动表 → 全部交回自动
    assert cap.tier_overrides({}) == {} and cap.tier_overrides(None) == {}


def test_out_chars_backfill_for_old_rows(tmp_path, monkeypatch):
    """老库补 `out_chars` 列时，顺手把中断备注里的字符数抠回新列（只跑一次）。

    为什么值得回头填：那批老行都是「客户端提前断开」的，界面 Tokens 列本来只显示「—」，
    而行 hover 里却有 17374 —— 两边对不上，用户 2026-09-17 直接质疑过。不回填的话
    要等下一次中断才有新样本，老行永远看着像坏的。"""
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    # 老库表结构：没有 out_chars 列（与线上加列前一致）
    old.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
                " channel_id TEXT, channel_name TEXT, model TEXT, prompt_tokens INTEGER DEFAULT 0,"
                " completion_tokens INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,"
                " success INTEGER DEFAULT 1, error TEXT, upstream_model TEXT DEFAULT '',"
                " cancelled INTEGER DEFAULT 0, superseded INTEGER DEFAULT 0)")
    old.execute("INSERT INTO usage (ts, channel_name, model, success, error, cancelled)"
                " VALUES (1, 'NVIDIA NIM', 'auto-quality', 1,"
                " '客户端中断（模型已正常输出 17374 字符后断开）', 1)")
    # 一个字都没输出的中断行：不该被填成别的数（保持 0 → 界面显示「—」）
    old.execute("INSERT INTO usage (ts, channel_name, model, success, error, cancelled)"
                " VALUES (2, 'NVIDIA NIM', 'auto-quality', 0,"
                " '客户端中断（模型未输出内容）', 1)")
    old.commit()
    old.close()

    monkeypatch.setattr(store, "DB_PATH", str(db))
    store.init()                       # 加列 + 回填

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {r["id"]: r["out_chars"] for r in con.execute("SELECT id, out_chars FROM usage")}
    assert rows[1] == 17374, rows
    assert rows[2] == 0, rows
    logs = {l["id"]: l for l in store.recent(limit=5)["logs"]}
    assert logs[1]["out_chars"] == 17374 and logs[1]["cancelled"] == 1


def test_probe_body_check_and_403_levels():
    """两条审计发现：① 探测只认 200 不够，要看 body；② 403 分账号级 / 模型级。

    ① 上游拿 200 包错误对象、或给空 choices 时，旧版会把「假可用」模型标绿并放进 auto 候选。
    ② 单个模型没权限（付费/地区）不该把整渠道停掉 —— 判据是「渠道最近有没有成功过」。"""
    import time as _t

    from app.main import _probe_body_ok

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            if isinstance(self._p, Exception):
                raise self._p
            return self._p

    # 正常响应 → 放行。注意 content 为空也放行：推理模型 max_tokens=1 时就是这样，
    # 它不代表模型不可用（真用起来 max_tokens 大得多）
    ok = {"object": "chat.completion",
          "choices": [{"index": 0, "message": {"role": "assistant", "content": ""},
                       "finish_reason": "length"}]}
    assert _probe_body_ok(FakeResp(ok)) == ""
    assert "错误对象" in _probe_body_ok(FakeResp({"error": {"message": "boom"}}))
    assert "choices" in _probe_body_ok(FakeResp({"choices": []}))
    assert "不是 JSON" in _probe_body_ok(FakeResp(ValueError("bad json")))
    assert "结构异常" in _probe_body_ok(FakeResp(["a"]))

    # 账号级 vs 模型级 403
    _reset()
    assert not gateway.channel_recently_ok("c1")                 # 从没成功过 → 账号级（停渠道）
    gateway.channel_last_ok["c1"] = _t.time() - 5
    assert gateway.channel_recently_ok("c1")                     # 刚成功过 → 模型级（只标该模型）
    gateway.channel_last_ok["c1"] = _t.time() - 3600
    assert not gateway.channel_recently_ok("c1")                 # 过期 → 又算账号级
    gateway.channel_last_ok.clear()


def test_sse_marks_and_stream_outcome():
    """流式成败看「语义结束标记」，不是「上游 TCP 流关没关干净」。

    背景：魔搭发完数据后不马上关连接，客户端读完答案就关连接 → 网关卡在等最后几个字节时
    被取消，旧版据此把 80 条**已完成**的请求记成「客户端断开」的失败。
    第三个返回值（内容字符数）用来证明「客户端断开时模型到底吐没吐东西」。"""
    from app.main import _sse_line_marks, _stream_ok

    # 增量块：既不是结束也没有 usage，但要能数出内容字符数（"你" 是一个字符）
    assert _sse_line_marks(b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}') == (None, False, 1)
    # finish_reason 非空 → 结束
    assert _sse_line_marks(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}') == (None, True, 0)
    # finish_reason 为 null → 不是结束（别误判）
    assert _sse_line_marks(
        b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}') == (None, False, 1)
    # 推理模型的 reasoning_content 也算在干活（它确实在输出）
    assert _sse_line_marks(
        b'data: {"choices":[{"delta":{"reasoning_content":"abc"}}]}') == (None, False, 3)
    # usage 收尾块 → 结束 + 取到 usage
    u, end, chars = _sse_line_marks(
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}')
    assert end and u["completion_tokens"] == 20
    # [DONE]
    assert _sse_line_marks(b"data: [DONE]") == (None, True, 0)
    # 非 data 行、坏 JSON、空 usage、空 delta 都不该误判
    assert _sse_line_marks(b"event: ping") == (None, False, 0)
    assert _sse_line_marks(b"data: {oops") == (None, False, 0)
    assert _sse_line_marks(b'data: {"choices":[],"usage":{}}') == (None, False, 0)
    assert _sse_line_marks(b'data: {"choices":[{"delta":{}}]}') == (None, False, 0)

    # 判定表：(completed, saw_end, aborted, out_chars) → (ok, cancelled)
    assert _stream_ok(True, False, False) == (True, False)            # 正常读完
    assert _stream_ok(False, True, True) == (True, False)             # 拿到结束标记后被取消 = 收尾竞态 → 成功
    assert _stream_ok(False, True, False) == (True, False)            # [DONE] 之后上游掐线 → 仍算成功
    assert _stream_ok(False, False, True, 0) == (False, True)         # 一个字没出就断 = 真中断
    assert _stream_ok(False, False, True, 128) == (True, True)        # 有输出才断 = 模型正常，客户端先走 → 成功
    assert _stream_ok(False, False, False) == (False, False)          # 上游断流（异常路径）→ 失败


def test_stream_gen_accounts_end_marker_before_client_closes(monkeypatch):
    """回归（2026-09-17）：客户端「收到带 finish_reason 的那一块就断连」→ 必须记成功。

    用户报「Hermes 一切正常，网关日志里却有已中断」。根因是同一个 chunk 先 yield 给客户端、
    后解析：客户端拿到结束标记就断开，生成器被关掉，解析那一步永远轮不到 ——
    **已经送给客户端的结束标记**在账上等于不存在。修法：先解析再转发。"""
    import asyncio
    import time as _t

    from app.main import _stream_gen

    monkeypatch.setattr(gateway, "mark_ttft", lambda *a, **k: None)
    monkeypatch.setattr(gateway, "mark_result", lambda *a, **k: None)
    store.init()

    payload = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
               b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
               b'data: [DONE]\n\n')

    class FakeResp:
        async def aiter_bytes(self):
            yield payload
            await asyncio.sleep(30)     # 客户端已断，正常不会走到这

        async def aclose(self):
            pass

    async def consume_one_chunk_then_close():
        agen = _stream_gen(FakeResp(), "c1", "ch1", "m", _t.time(), "m")
        chunk = await agen.__anext__()          # 客户端收到含 finish_reason 的那一块
        assert b"finish_reason" in chunk
        await agen.aclose()                     # 立刻关连接（Hermes 的行为）

    asyncio.run(consume_one_chunk_then_close())
    log = store.recent(limit=1)["logs"][0]
    assert log["success"] == 1, log             # 客户端拿到的是完整应答
    assert log["cancelled"] == 0, log           # 答完才关连接 ≠ 中断，不该打标注


def test_superseded_attempt_still_counts_as_failure(monkeypatch, tmp_path):
    """统计按「模型×尝试」记：这一跳失败就是失败，**不因后面换路成功而豁免**。

    `superseded` 只是补充信息（这一跳之后请求续到了别的候选），不改变失败归属。
    —— 用户 2026-09-17 明确的口径：「我们统计的就是具体的模型是失败还是成功还是其它什么」。"""
    from app import store as st

    st.init()
    # 第一次尝试（渠道 A）失败，后面还有候选 → superseded=1，但仍算失败
    st.log_usage("c1", "A", "m", 0, 0, 56400, False, "connect: Server disconnected",
                 upstream_model="m", superseded=True)
    # 换到渠道 B 成功
    st.log_usage("c2", "B", "m", 3, 400, 900, True, upstream_model="m")

    s = st.summary(days=1)
    assert s["totals"]["requests"] == 2
    assert s["totals"]["errors"] == 1, s["totals"]          # A 那一跳失败 → 算
    assert s["totals"]["superseded"] == 1                   # 并且标出「后面换过路」
    assert s["daily"][0]["errors"] == 1
    assert s["daily"][0]["superseded"] == 1
    assert [c["errors"] for c in s["by_channel"] if c["name"] == "A"] == [1]

    # 客户端主动中断不算失败（与 superseded 是两回事）
    st.log_usage("c3", "C", "m", 0, 0, 1200, False, "客户端中断（无结束标记）",
                 upstream_model="m", cancelled=True)
    s2 = st.summary(days=1)
    assert s2["totals"]["errors"] == 1 and s2["totals"]["cancelled"] == 1
    assert st.recent(limit=1)["logs"][0]["superseded"] == 0


def test_stream_gen_records_race_as_success(tmp_path, monkeypatch):
    """端到端：读完 [DONE] 后连接被客户端关掉 → 记成功；
    只吐了一块有内容、没结束标记就被掐 → **也算成功**（模型在正常输出，客户端先走）；
    一个字都没输出就被掐 → 「已中断」（既不是成功也不是失败）。"""
    import asyncio
    import time as _t

    from app.main import _stream_gen
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "race.db"))
    monkeypatch.setattr(gateway, "mark_ttft", lambda *a, **k: None)
    marked = []
    monkeypatch.setattr(gateway, "mark_result", lambda *a, **k: marked.append(a))
    store.init()

    full = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":9}}\n\n'
            b'data: [DONE]\n\n')
    half = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    empty = b'data: {"choices":[{"delta":{}}]}\n\n'      # 只有心跳式空块，没有内容

    class FakeResp:
        def __init__(self, payload):
            self.payload = payload

        async def aiter_bytes(self):
            yield self.payload
            raise asyncio.CancelledError()      # 客户端在这一刻关掉了连接

        async def aclose(self):
            pass

    async def run(payload):
        async for _ in _stream_gen(FakeResp(payload), "c1", "ch1", "m", _t.time(), "m"):
            pass

    # A：完整读完（含 usage + [DONE]）后连接被关 → 请求其实是成功的
    try:
        asyncio.run(run(full))
    except asyncio.CancelledError:
        pass
    log = store.recent(limit=1)["logs"][0]
    assert log["success"] == 1, log
    assert log["cancelled"] == 0, log
    assert log["completion_tokens"] == 9
    assert log["error"] is None
    assert marked and marked[-1][2] is True          # mark_result(..., ok=True)

    # B：吐了一块内容、没有结束标记就被掐 → 模型没问题（有输出），算成功；
    #    没有结束标记、但断过一次 → cancelled=1 只作为「被客户端中断过」的标注
    try:
        asyncio.run(run(half))
    except asyncio.CancelledError:
        pass
    log = store.recent(limit=1)["logs"][0]
    assert log["success"] == 1, log
    assert log["cancelled"] == 1, log
    assert "已正常输出" in (log["error"] or "") and "2 字符" in (log["error"] or ""), log["error"]
    assert log["out_chars"] == 2, log               # 「hi」= 2 字符，单独入库供 Tokens 列显示
    assert marked[-1][2] is True                     # 模型正常 → 记正样本

    # C：一个字都没输出就被掐 → 「已中断」：不算成功也不算失败，且不写稳定分
    n_marked = len(marked)
    try:
        asyncio.run(run(empty))
    except asyncio.CancelledError:
        pass
    log = store.recent(limit=1)["logs"][0]
    assert log["success"] == 0 and log["cancelled"] == 1, log
    assert "未输出内容" in (log["error"] or ""), log["error"]
    assert log["out_chars"] == 0, log                # 一个字都没吐 → 界面 Tokens 列显示「—」
    assert len(marked) == n_marked                   # 没输出 → 不是模型的账，不进稳定分


def test_stream_success_sets_session_sticky(tmp_path, monkeypatch):
    """**流式**成功产出才写会话粘性（上游返回 200 ≠ 产出成功）。

    口径与记账同源：客户端断开但模型已正常输出 → 算成功产出（该继续粘着）；
    一个字都没吐就断 → 不算（不粘，也不覆盖已有的粘性）。"""
    import asyncio
    import time as _t

    from app.main import _stream_gen
    _reset()
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sticky_stream.db"))
    monkeypatch.setattr(gateway, "mark_ttft", lambda *a, **k: None)
    monkeypatch.setattr(gateway, "mark_result", lambda *a, **k: None)
    store.init()

    full = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":9}}\n\n'
            b'data: [DONE]\n\n'
            b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n')   # 结束标记之后再吐一小块
    half = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'     # 有内容、没结束标记
    empty = b'data: {"choices":[{"delta":{}}]}\n\n'                  # 一个字都没有

    class FakeResp:
        def __init__(self, payload):
            self.payload = payload

        async def aiter_bytes(self):
            yield self.payload
            raise asyncio.CancelledError()      # 客户端在这一刻关掉了连接

        async def aclose(self):
            pass

    async def run(payload):
        async for _ in _stream_gen(FakeResp(payload), "c1", "ch1", "m", _t.time(), "m",
                                   sticky_scope=("quality", "sess9", "重构代码")):
            pass

    try:
        asyncio.run(run(full))
    except asyncio.CancelledError:
        pass
    ent = gateway.sticky.get(("quality", "sess9"))
    assert ent and ent["model"] == "m" and ent["channel"] == "c1", ent
    assert ent["label"] == "重构代码", ent

    # 有输出但被客户端掐断 → 模型没问题，粘性继续（换个模型做对照，确认是被覆盖而不是没动）
    gateway.sticky[("quality", "sess9")]["model"] = "other"
    try:
        asyncio.run(run(half))
    except asyncio.CancelledError:
        pass
    assert gateway.sticky[("quality", "sess9")]["model"] == "m", gateway.sticky

    # 一个字没吐就被掐 → 不粘：已有的粘性必须原样留着（不能被一次「没输出」改写）
    gateway.clear_sticky()
    try:
        asyncio.run(run(empty))
    except asyncio.CancelledError:
        pass
    assert ("quality", "sess9") not in gateway.sticky, gateway.sticky


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
    # model-a：c1 上硬失败（402）→ 该模型只有这一条渠道 → down
    # ⚠️ 2026-09-21（A1）起必须用**渠道级**的 mark_channel_down 来造硬失败：模型级
    #    mark_model_status 已不再参与可用性判定，用它造的场景测不到真实语义（真实路径里
    #    两者总是成对写的：先 mark_channel_down 记渠道，再 mark_model_status 记展示原因）。
    gateway.mark_channel_down("model-a", "c1", "HTTP 402")
    # model-b：429 限流 → limited（(模型,渠道) 冷却中）
    gateway.mark_model_status("model-b", True, "限流", "c1", state="limited")
    gateway.cooldown[("model-b", "c1")] = _t.time() + 300
    # model-c：正常 → ok
    gateway.mark_model_status("model-c", True, "", "c2", state="ok")
    view = {m["id"]: m for m in gateway.model_view(cfg)}
    assert view["model-a"]["status"] == "down" and view["model-a"]["available"] is False
    assert view["model-b"]["status"] == "limited" and view["model-b"]["available"] is False
    assert view["model-c"]["status"] == "ok" and view["model-c"]["available"] is True
    # A1 新增字段：渠道 chip 的悬停原因（只在不可用时非空）
    assert "402" in view["model-a"]["channels"][0]["unavail_reason"]
    assert view["model-c"]["channels"][0]["unavail_reason"] == ""


def test_model_view_channel_status_ignores_model_level_down():
    """A1 核心回归：一次渠道硬失败（连带写下模型级 down）不得把该模型**其他渠道**判红。

    真实受害者（2026-09-21 从线上 overview + 磁盘记录逐条核出来的 3 个）：
      - `z-ai/glm-5.3`：OpenRouter 判「非免费」→ 模型级 down → 把 NVIDIA NIM 那条
        **从没失败过**的渠道一起显示不可用；
      - `stepfun-ai/Step-3.5-Flash` / `-3.7-Flash`：403 记录来自 HuggingFace，而该渠道
        已停用（`model_view` 直接跳过它）→ 那笔模型级 down 继续压着它们**唯一还启用的**
        魔搭渠道。注意 `gateway.py` 的 cleanup 只清 channel_down 这类渠道级字典，
        **不清 model_status** —— 这是渠道增删后污染会复现的机制性原因。

    旧行为：`model_view` 里渠道 available 末尾带 `and not model_down` → 所有渠道一起红。
    """
    _reset()
    gateway.channels.clear()
    _chan("cx", ["model-x"], latency=100)
    _chan("cy", ["model-x"], latency=200)
    cfg = _cfg([{"id": "cx", "name": "cx", "type": "custom", "base_url": "http://x/v1",
                 "api_key": "k", "enabled": True},
                {"id": "cy", "name": "cy", "type": "custom", "base_url": "http://x/v1",
                 "api_key": "k", "enabled": True}])
    gateway.mark_channel_down("model-x", "cx", "HTTP 402: no credit")
    gateway.mark_model_status("model-x", False, "HTTP 402: no credit", "cx", state="down")

    m = {x["id"]: x for x in gateway.model_view(cfg)}["model-x"]
    per = {c["channel_id"]: c for c in m["channels"]}
    assert per["cx"]["available"] is False and per["cx"]["down"] is True
    assert per["cy"]["available"] is True, "另一条渠道不该被模型级 down 压红"
    assert "402" in per["cx"]["unavail_reason"]
    assert per["cy"]["unavail_reason"] == ""
    assert m["status"] == "ok" and m["available"] is True, "有渠道可用 → 模型整体可用"
    # 展示字段仍在（说明「最近一次在哪条渠道测出什么」），只是不再参与判定
    assert m["tested"] is True and m["test_channel"] == "cx"


def test_candidates_for_auto_ignores_model_level_down():
    """A1：模型级 down 不得把该模型在**其他渠道**上的候选一起踢掉（否则健康渠道挨饿）。"""
    _reset()
    gateway.channels.clear()
    _chan("cx", ["model-y"], latency=100)
    _chan("cy", ["model-y"], latency=200)
    cfg = _cfg([{"id": "cx", "name": "cx", "type": "custom", "base_url": "http://x/v1",
                 "api_key": "k", "enabled": True},
                {"id": "cy", "name": "cy", "type": "custom", "base_url": "http://x/v1",
                 "api_key": "k", "enabled": True}])
    gateway.mark_channel_down("model-y", "cx", "HTTP 403")
    gateway.mark_model_status("model-y", False, "HTTP 403", "cx", state="down")

    cids = {c["channel"]["id"] for c in gateway.candidates_for_auto("balanced", cfg)
            if c["model"] == "model-y"}
    assert cids == {"cy"}, cids


def test_channel_available_models_ignores_model_level_down():
    """A1：渠道卡片的「可用 N 个」要和真实可路由的路径同口径，别少算。"""
    _reset()
    gateway.channels.clear()
    _chan("cx", ["model-z", "model-w"], latency=100)
    gateway.mark_model_status("model-z", False, "HTTP 402", "cx", state="down")
    assert gateway.channel_available_models("cx") == 2
    # 渠道级硬失败照样要减（这条才是真不可路由）
    gateway.mark_channel_down("model-z", "cx", "HTTP 402")
    assert gateway.channel_available_models("cx") == 1


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


def test_restore_keeps_daily_quota_limited():
    """「每日额度冷却」的 limited 不该在重启时被升成永久 down。

    为什么单独守这条：`_PERMANENT_KEYWORDS`（判**上游正文**用）里有「已用完 / 用尽」，
    而我们自己写的每日额度文案正是「…该模型额度已用完，冷却到明天」；OpenRouter 免费模型的
    日额度正文里还有一句 `Add 10 credits to unlock 1000 free model requests per day`
    —— 命中同表里的裸 `credit`。启动归正**共用**那套关键词 → 每次重启都把「明天就恢复」
    升成永久 down（down 没有出口 → 再也回不来），静默撤销额度 429 的修复。
    归正现在改用更窄的 `_DOWN_REASON_MARKERS`。
    """
    import time as _t
    import json, tempfile, os
    from app import store
    now = _t.time()
    gateway.model_status.clear()
    gateway.model_status = {
        # 我们自己写的每日额度文案（今天新增的额度 429 降级路径）
        "ms/q": {"state": "limited", "available": False, "ts": now,
                 "reason": "免费额度用完 (HTTP 429)：该模型额度已用完，冷却到明天"
                           "（渠道其它模型不受影响）"},
        # 旧版文案（classify_429 的 free_daily note）
        "old/q": {"state": "limited", "available": False, "ts": now,
                  "reason": "额度用尽，冷却至明天"},
        # OpenRouter 免费模型的日额度正文（含 "Add 10 credits"）
        "or/free": {"state": "limited", "available": False, "ts": now,
                    "reason": 'HTTP 429: {"error":{"message":"Rate limit exceeded: '
                              'free-models-per-day. Add 10 credits to unlock 1000 free '
                              'model requests per day","code":429}}'},
        # 真·余额不足：仍应归正为 down
        "glm-4.5": {"state": "limited", "available": True, "ts": now,
                    "reason": "余额不足，请充值"},
    }
    store._ms_cache = None
    old_path = store.MODEL_STATUS_PATH
    tmp = os.path.join(tempfile.gettempdir(), "ms_daily.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gateway.model_status, f, ensure_ascii=False)
    store.MODEL_STATUS_PATH = tmp
    try:
        store._ms_cache = None
        gateway.model_status.clear()
        gateway.restore_model_status()
        for k in ("ms/q", "old/q", "or/free"):
            assert gateway.model_status[k]["state"] == "limited", \
                f"{k} 被误升成 {gateway.model_status[k]['state']}：{gateway.model_status[k]['reason'][:80]}"
            assert gateway.model_status[k]["available"] is False
        assert gateway.model_status["glm-4.5"]["state"] == "down"
    finally:
        store.MODEL_STATUS_PATH = old_path
        store._ms_cache = None
        os.remove(tmp)
    gateway.model_status.clear()


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
    """auto-<视图名> 与「模型」页视图一一对应；裸 `auto` 与冒号版（auto:balanced 等）
    均已删除 —— 2026-09-21 用户拍板：冒号不规范，只保留连字符写法"""
    from app.gateway import list_reserved_auto, auto_strategy_of, is_reserved_auto
    assert auto_strategy_of("auto-balanced") == "balanced"
    assert auto_strategy_of("auto-vision") == "vision"
    assert is_reserved_auto("auto-balanced") and is_reserved_auto("auto-vision")
    assert not is_reserved_auto("auto")            # 裸 auto 当普通模型名查（查不到 → 404）
    assert not is_reserved_auto("auto:balanced")   # 冒号版同样已删（→ 404）
    assert sorted(list_reserved_auto()) == ["auto-balanced", "auto-quality",
                                            "auto-speed", "auto-stability", "auto-vision"]
    # 名字不含非法字符（codex 的 [A-Za-z0-9._/-] 白名单）
    assert auto_strategy_of("auto-quality") == "quality"
    assert auto_strategy_of("auto-balanced") == "balanced"
    import re
    assert all(re.fullmatch(r"[A-Za-z0-9._/-]+", m) for m in list_reserved_auto())


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
def test_auto_vision_has_its_own_pin_list():
    """视觉收藏是独立的一套（2026-09-20 用户要求）：`pinned_vision` 只管视觉视图与 auto-vision，
    `pinned` 只管其余四个视图与对应策略——两表互不影响，同名模型要两边各收一次。"""
    _reset()
    gateway.model_status.clear()
    chans = [_chan("strong", ["qwen-test-vl-72b"], latency=300),
             _chan("weak", ["test-vision-small"], latency=2700)]
    base = {**_cfg(chans, strategy="vision"),
            "model_tier_exact": {"qwen-test-vl-72b": 3, "test-vision-small": 1}}
    # 无收藏 → 能力分高的在前（强 = 智能档 1.0 / 弱 = 轻量档 0.2，手动档位口径 ①）
    assert [c["model"] for c in gateway.candidates_for_auto("vision", base)][:2] == \
        ["qwen-test-vl-72b", "test-vision-small"]
    # 主收藏对视觉视图无效（视觉视图读自己那份）
    got = gateway.candidates_for_auto("vision", {**base, "pinned": ["test-vision-small"]})
    assert got[0]["model"] == "qwen-test-vl-72b", got[0]["model"]
    # 视觉专属收藏才管这一屏
    got = gateway.candidates_for_auto("vision", {**base, "pinned_vision": ["test-vision-small"]})
    assert got[0]["model"] == "test-vision-small", got[0]["model"]
    # 反向：视觉收藏不影响 quality；主收藏照旧管 quality
    got = gateway.candidates_for_auto("quality", {**base, "pinned_vision": ["test-vision-small"]})
    assert got[0]["model"] == "qwen-test-vl-72b", got[0]["model"]
    got = gateway.candidates_for_auto("quality", {**base, "pinned": ["test-vision-small"]})
    assert got[0]["model"] == "test-vision-small", got[0]["model"]


def test_auto_vision_only_keeps_vision_models():
    """`auto-vision` 只留能看图的模型（Hermes 辅助视觉模型用）——回归：别把文本模型混进来"""
    _reset()
    gateway.model_status.clear()
    chans = [_chan("c", ["qwen-test-vl-72b", "test-plain-text-70b"], latency=300)]
    got = gateway.candidates_for_auto("vision", _cfg(chans, strategy="vision"))
    assert [c["model"] for c in got] == ["qwen-test-vl-72b"], [c["model"] for c in got]


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
    # 收藏 deepseek：auto-quality 必须先试收藏的 deepseek（即使更慢）
    got2 = gateway.candidates_for_auto(
        "quality", {**_cfg(chans, strategy="quality"), "pinned": ["deepseek-v4-pro"]})
    assert got2[0]["model"] == "deepseek-v4-pro", \
        f"收藏的模型必须在候选最前，实际 {got2[0]['model']}"


# ---------------- 会话粘性（2026-09-20）：同一会话里模型健康就不该换模型 ----------------
def test_session_key_follows_first_user_message():
    """会话键认的是「第一条 user 消息」：客户端每轮都发完整历史，所以
    ① 历史增长、② system 里的动态内容（日期）变化，都不该换键；换任务才换键。"""
    base = {"model": "auto-quality", "messages": [
        {"role": "system", "content": "你是助手。今天是 2026-09-20"},
        {"role": "user", "content": "帮我把这份代码重构一下"}]}
    k1, src, label = gateway.session_key_of(base)
    assert k1 and src == "first_user", (k1, src, label)
    assert label.startswith("帮我把这份代码重构"), label
    # 第二轮：system 里的日期变了 + 追加 assistant/user（多模态片段）→ 同一个键
    more = {"model": "auto-quality", "messages": [
        {"role": "system", "content": "你是助手。今天是 2026-09-21"},
        {"role": "user", "content": "帮我把这份代码重构一下"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": [{"type": "text", "text": "继续"},
                                     {"type": "image_url", "image_url": {"url": "x"}}]}]}
    assert gateway.session_key_of(more)[0] == k1
    # 换一个首条 user 消息 → 换键（另一个会话）
    assert gateway.session_key_of({"messages": [{"role": "user", "content": "另一个任务"}]})[0] != k1
    # 显式请求头最优先（客户端愿意发就用它）
    k_h, src_h, _ = gateway.session_key_of(more, "conv-abc-123")
    assert src_h == "header" and k_h != k1
    assert gateway.session_key_of({"messages": []}, "conv-abc-123")[0] == k_h   # 与消息内容无关
    # 没有 user 消息（少见）→ 退回「首条消息」指纹，来源标 first_msg
    k_sys, src_sys, _ = gateway.session_key_of(
        {"messages": [{"role": "system", "content": "只有系统提示"}]})
    assert k_sys and src_sys == "first_msg", (k_sys, src_sys)
    # 认不出（没有消息 / 认不出文本）→ 空键 = 不粘（安全降级成老行为）
    assert gateway.session_key_of({"messages": []})[0] == ""
    assert gateway.session_key_of({"messages": [{"role": "user", "content": ""}]})[0] == ""
    assert gateway.session_key_of({"messages": ["不是字典"]})[0] == ""
    assert gateway.session_key_of({})[0] == ""


def test_auto_sticky_beats_pinned_within_session():
    """**粘性 > 收藏**：本会话上一次成功的模型排最前，即使另一个模型被收藏了。
    这正是用户报的现象（收藏的 glm 一过冷却就抢回第一 → 看着像「无故换模型」）。"""
    _reset()
    gateway.model_status.clear()
    chans = [_chan("slow", ["deepseek-v4-pro"], latency=2700),
             _chan("fast", ["qwen3.8-235b"], latency=300)]
    cfg = {**_cfg(chans, strategy="quality"), "pinned": ["deepseek-v4-pro"]}
    assert gateway.candidates_for_auto("quality", cfg)[0]["model"] == "deepseek-v4-pro"  # 收藏优先（原行为）
    gateway.mark_sticky("quality", "sess1", "qwen3.8-235b", "fast", label="重构代码")
    got, hit = gateway.prefer_sticky(gateway.candidates_for_auto("quality", cfg), "quality", "sess1")
    assert got[0]["model"] == "qwen3.8-235b", [c["model"] for c in got[:2]]
    assert hit and hit["model"] == "qwen3.8-235b" and hit["idle_s"] >= 0
    # 作用域：别的会话、别的策略都不受影响（auto-vision 与主策略各粘各的）
    assert gateway.prefer_sticky(gateway.candidates_for_auto("quality", cfg), "quality", "sess2")[1] is None
    assert gateway.prefer_sticky(gateway.candidates_for_auto("quality", cfg), "speed", "sess1")[1] is None
    assert len(gateway.sticky_view()) == 1


def test_sticky_keeps_model_when_channel_cools_drops_when_model_gone():
    """粘的是**模型**不是渠道：原渠道抖了（冷却）就换同模型的另一个渠道，
    不该因为渠道问题把整个会话换到别的模型上；模型整个不可用才解除粘性、回策略排序。"""
    import time as _t
    _reset()
    gateway.model_status.clear()
    chans = [_chan("cA", ["qwen3.8-235b"], latency=300),
             _chan("cB", ["qwen3.8-235b"], latency=2000),
             _chan("cC", ["deepseek-v4-pro"], latency=100)]
    cfg = _cfg(chans, strategy="quality")
    gateway.mark_sticky("quality", "s1", "qwen3.8-235b", "cA")
    got, hit = gateway.prefer_sticky(gateway.candidates_for_auto("quality", cfg), "quality", "s1")
    assert (got[0]["model"], got[0]["channel"]["id"]) == ("qwen3.8-235b", "cA")
    assert hit and hit["switched_channel"] is False
    gateway.cooldown[("qwen3.8-235b", "cA")] = _t.time() + 300      # 原渠道冷却
    got2, hit2 = gateway.prefer_sticky(gateway.candidates_for_auto("quality", cfg), "quality", "s1")
    assert (got2[0]["model"], got2[0]["channel"]["id"]) == ("qwen3.8-235b", "cB"), \
        [(c["model"], c["channel"]["id"]) for c in got2[:2]]
    assert hit2 and hit2["switched_channel"] is True
    gateway.cooldown[("qwen3.8-235b", "cB")] = _t.time() + 300      # 模型全渠道不可用
    got3, hit3 = gateway.prefer_sticky(gateway.candidates_for_auto("quality", cfg), "quality", "s1")
    assert hit3 is None and got3[0]["model"] == "deepseek-v4-pro", got3[0]["model"]
    assert ("quality", "s1") not in gateway.sticky                   # 失效条目顺手删掉


def test_sticky_expires_and_is_clearable():
    """闲置超 TTL 就失效（会话结束了）；`clear_sticky` 是「立刻换模型」的兜底手段。"""
    import time as _t
    _reset()
    chans = [_chan("c1", ["qwen3.8-235b"], latency=300)]
    cfg = _cfg(chans, strategy="quality")
    cands = gateway.candidates_for_auto("quality", cfg)
    gateway.mark_sticky("quality", "old", "qwen3.8-235b", "c1")
    gateway.sticky[("quality", "old")]["ts"] = _t.time() - gateway.STICKY_TTL - 1
    assert gateway.prefer_sticky(cands, "quality", "old")[1] is None
    assert ("quality", "old") not in gateway.sticky and gateway.sticky_view() == []
    gateway.mark_sticky("quality", "s1", "qwen3.8-235b", "c1", label="任务 A")
    gateway.mark_sticky("vision", "s1", "qwen3.8-235b", "c1")
    assert {v["strategy"] for v in gateway.sticky_view()} == {"quality", "vision"}
    assert gateway.sticky_view()[0]["label"] == "任务 A"
    assert gateway.clear_sticky("vision") == 1 and ("quality", "s1") in gateway.sticky
    assert gateway.clear_sticky() == 1 and not gateway.sticky


def test_sticky_persists_across_restart():
    """重启后同一个会话继续用同一个模型（不然一重启就换模型，白粘）"""
    import time as _t
    _reset()
    gateway.mark_sticky("quality", "s1", "qwen3.8-235b", "c1", label="重构代码")
    gateway.save_runtime_state()
    gateway.sticky.clear()
    gateway.restore_runtime_state()
    ent = gateway.sticky.get(("quality", "s1"))
    assert ent and ent["model"] == "qwen3.8-235b" and ent["label"] == "重构代码", ent
    # 已过期的粘性不该被恢复
    gateway.sticky[("quality", "old")] = {"model": "x", "channel": "c1", "label": "",
                                          "ts": _t.time() - gateway.STICKY_TTL - 10}
    gateway.save_runtime_state()
    gateway.sticky.clear()
    gateway.restore_runtime_state()
    assert ("quality", "old") not in gateway.sticky
    assert ("quality", "s1") in gateway.sticky


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


# ---------------- 上游官方限流响应头：魔搭两层 + 通用 x-ratelimit-* ----------------
class _Headers(dict):
    """响应头桩：模拟 httpx.Headers 的**大小写不敏感**取值。

    真实路上传进来的是 httpx.Headers，头名大小写由上游定。用普通 dict 打桩会让
    `get("modelscope-...")` 对 `Modelscope-Ratelimit-...` 取不到值 —— 测出来的失败是假的。"""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def test_ratelimit_modelscope_two_levels():
    """魔搭那 4 个头分两层落两张表：模型级 → (模型,渠道)；账号级 → 渠道。

    上限也必须取自响应头：官方会**动态调整**单模型上限（通用 500/天，大模型只有 100/天，
    濒临下线还会再降），写死 500 会误判。"""
    import time as _t
    _reset()
    mid, cid = "Qwen/Qwen3.8-Flash-Next", "c_ms"
    assert gateway.note_ratelimit_headers(cid, mid, _Headers({
        "Modelscope-Ratelimit-Requests-Limit": "2000",
        "Modelscope-Ratelimit-Requests-Remaining": "1873",
        "Modelscope-Ratelimit-Model-Requests-Limit": "500",
        "Modelscope-Ratelimit-Model-Requests-Remaining": "42",
    })) is True
    st = gateway.ratelimit[(mid, cid)]
    assert (st["remaining"], st["limit"], st["source"]) == (42, 500, "modelscope")
    assert 0 < st["reset_ts"] - _t.time() <= 86400 + 60        # 指向下一个 UTC+8 零点
    assert gateway.user_quota[cid]["remaining"] == 1873
    assert gateway.user_quota[cid]["limit"] == 2000
    assert gateway.ratelimit_hits["modelscope"] == 1
    assert gateway.ratelimit_exhausted(mid, cid) is False      # 还剩 42 次 → 照常路由


def test_ratelimit_modelscope_model_exhausted_skips_in_routing():
    """端到端：**模型级**额度归零 → 候选里直接不再出现，不用等它撞 429 才知道"""
    _reset()
    cid = "c_ms"
    ch = _chan(cid, ["Qwen/Qwen3.8-Flash-Next"])
    cfg = _cfg([ch])
    assert gateway.candidates_for("Qwen/Qwen3.8-Flash-Next", cfg)          # 归零前有候选
    gateway.note_ratelimit_headers(cid, "Qwen/Qwen3.8-Flash-Next", _Headers({
        "modelscope-ratelimit-model-requests-remaining": "0"}))
    assert gateway.candidates_for("Qwen/Qwen3.8-Flash-Next", cfg) == []
    # 手动测试候选同样跳过：额度是真没了，点按钮也只会白烧一次
    assert gateway.candidates_for_test("Qwen/Qwen3.8-Flash-Next", cfg) == []


def test_ratelimit_modelscope_account_exhausted_cools_channel():
    """**账号级**归零 = 该 Key 当天用完 → 整条渠道停到明天（复用 mark_channel_quota_exhausted），
    不必等第二个模型也撞 429 才熔断。"""
    import time as _t
    _reset()
    cid = "c_ms"
    assert gateway.note_ratelimit_headers(cid, "m", _Headers({
        "modelscope-ratelimit-requests-remaining": "0",
        "modelscope-ratelimit-requests-limit": "2000"})) is True
    assert gateway.channel_cooling(cid) is True
    assert 599 <= gateway.channel_cool[cid] - _t.time() <= 86400    # 599：临近午夜的下限夹取


def test_ratelimit_reset_ts_never_zero():
    """回归：`reset_ts=0` 会让那一格被**永久**跳过（`ratelimit_exhausted` 只在
    「正数且已过期」时才解除）。魔搭头不给重置时刻、通用头也可能缺失 → 两条路都不许留 0。"""
    import time as _t
    _reset()
    gateway.note_ratelimit_headers("c", "m_ms", _Headers({
        "modelscope-ratelimit-model-requests-remaining": "0"}))
    assert gateway.ratelimit[("m_ms", "c")]["reset_ts"] > _t.time()
    gateway.note_ratelimit_headers("c", "m_x", _Headers({
        "x-ratelimit-remaining-requests": "0"}))        # 故意不带 reset 头
    assert gateway.ratelimit[("m_x", "c")]["reset_ts"] > _t.time()


def test_next_ms_daily_reset_is_next_utc8_midnight():
    import calendar
    import time as _t
    base = calendar.timegm((2026, 9, 20, 12, 0, 0, 0, 0, 0))   # 12:00 UTC = 20:00 UTC+8
    r = gateway._next_ms_daily_reset(base)
    assert r - base == 4 * 3600 + 60
    g = _t.gmtime(r - 60 + 8 * 3600)                           # 去掉缓冲、平移回 UTC+8
    assert (g.tm_hour, g.tm_min, g.tm_sec) == (0, 0, 0)


def test_ratelimit_x_headers_path_unchanged():
    """通用 x-ratelimit-* 老路径照旧（Groq/NIM），命中计数按口径分开数"""
    import time as _t
    _reset()
    assert gateway.note_ratelimit_headers("c_nim", "glm-5.3-flash", _Headers({
        "x-ratelimit-remaining-requests": "7",
        "x-ratelimit-reset-requests": "2m30s"})) is True
    st = gateway.ratelimit[("glm-5.3-flash", "c_nim")]
    assert (st["remaining"], st["source"]) == (7, "x-ratelimit")
    assert 148 <= st["reset_ts"] - _t.time() <= 152
    assert gateway.ratelimit_hits == {"modelscope": 0, "x-ratelimit": 1}


def test_ratelimit_non_numeric_header_degrades():
    """头值非数字（官方 parseHeaderInt 里就出现过 "unlimited"）→ 当作没这个头：
    不能抛异常，也不能污染状态/计数"""
    _reset()
    assert gateway.note_ratelimit_headers("c", "m", _Headers({
        "modelscope-ratelimit-model-requests-remaining": "unlimited"})) is False
    assert gateway.ratelimit == {} and gateway.user_quota == {}
    assert gateway.ratelimit_hits == {"modelscope": 0, "x-ratelimit": 0}


def test_ratelimit_user_quota_persists_across_restart():
    """账号级日额度要跨重启保留：重启后不该忘掉「今天还剩多少」，
    命中计数同理（否则重启后没发请求时看不出魔搭的头到底有没有被读到）"""
    _reset()
    gateway.note_ratelimit_headers("c_ms", "m", _Headers({
        "modelscope-ratelimit-requests-remaining": "1500",
        "modelscope-ratelimit-requests-limit": "2000"}))
    gateway.save_runtime_state()
    gateway.user_quota.clear()
    gateway.ratelimit_hits.update({"modelscope": 0, "x-ratelimit": 0})
    gateway.restore_runtime_state()
    assert gateway.user_quota["c_ms"]["remaining"] == 1500
    assert gateway.ratelimit_hits["modelscope"] == 1
