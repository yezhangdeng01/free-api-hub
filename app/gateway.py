"""运行时状态：渠道健康、模型注册表、评分排序、错误分类冷却"""
import time

from . import capability, providers, throttle

# 失败冷却秒数（按错误类型分类，借鉴 FreeLLMAPI 的智能路由思想）
COOLDOWN_SECONDS = {
    "rate_limit": 300,   # 429：限流，冷却 5 分钟（或按 Retry-After）
    "auth": 900,         # 401/403：Key 无效，冷却 15 分钟
    "client": 300,       # 其他 4xx：可能是参数/模型不支持，冷却 5 分钟
    "server": 60,        # 5xx：上游临时故障，冷却 1 分钟
    "connect": 120,      # 连接失败
}
DEFAULT_COOLDOWN = 120

cooldown: dict = {}   # (上游模型ID, channel_id) -> 冷却截止时间
stats: dict = {}      # (上游模型ID, channel_id) -> {"score": 0~1, "latency": EMA毫秒}
model_status: dict = {}  # model_id -> {"available": bool, "reason": str, "ts": float, "channel": str}


class ChannelState:
    def __init__(self):
        self.valid = None          # None=未检测 True/False
        self.error = None
        self.last_check = 0
        self.latency_ms = None
        self.models = []
        self.quota = None
        self.quota_ts = 0
        self.quota_error = None


# 全局状态表
channels: dict = {}


def get_cs(cid: str) -> ChannelState:
    if cid not in channels:
        channels[cid] = ChannelState()
    return channels[cid]


def sync_channels(cfg: dict):
    """配置变更后同步状态表，清掉已删除渠道的状态"""
    ids = {c["id"] for c in cfg["channels"]}
    for cid in list(channels):
        if cid not in ids:
            channels.pop(cid, None)
    for key in list(cooldown):
        if key[1] not in ids:
            cooldown.pop(key, None)
    for key in list(stats):
        if key[1] not in ids:
            stats.pop(key, None)


def mark_model_status(model: str, available: bool, reason: str = "", channel: str = "",
                      state: str = None):
    """记一次模型级测试/调用的真实结果并持久化。

    state: 'ok' 免费可调 / 'limited' 暂时受限（429/5xx/连接，冷却后会恢复）/ 'down' 硬不可用。
    available 与 state 联动：只有 'down' 才视作不可路由。"""
    if state is None:
        state = "ok" if available else "down"
    model_status[model] = {"available": state != "down", "state": state,
                           "reason": (reason or "")[:200],
                           "ts": time.time(), "channel": channel}
    try:
        from . import store
        store.persist_model_status(model, model_status[model])
    except Exception:
        pass  # 持久化失败不影响内存状态


def restore_model_status():
    """启动时从磁盘恢复已测状态（避免每次重启都要重新扫描）"""
    from . import store
    for mid, entry in store.load_model_status().items():
        if isinstance(entry, dict) and "available" in entry:
            model_status[mid] = entry


def get_model_status(model: str):
 return model_status.get(model)


def _update_score(model: str, cid: str, ok: bool, latency_ms=None):
    """滑动评分：成功 0.8*旧+0.2，失败 0.8*旧；延迟做 7:3 EMA"""
    s = stats.setdefault((model, cid), {"score": 0.7, "latency": None})
    s["score"] = round(s["score"] * 0.8 + (0.2 if ok else 0.0), 3)
    if ok and latency_ms:
        s["latency"] = latency_ms if s["latency"] is None else int(s["latency"] * 0.7 + latency_ms * 0.3)


def mark_result(model: str, cid: str, ok: bool, latency_ms=None,
                kind: str = None, retry_after: int = None):
    """记录一次真实请求结果：成功解除冷却并加分，失败按类型冷却并扣分"""
    _update_score(model, cid, ok, latency_ms)
    if ok:
        cooldown.pop((model, cid), None)
    else:
        seconds = COOLDOWN_SECONDS.get(kind, DEFAULT_COOLDOWN)
        if retry_after and retry_after > 0:
            seconds = max(seconds, min(retry_after, 3600))
        cooldown[(model, cid)] = time.time() + seconds


def get_stat(model: str, cid: str) -> dict:
    return stats.get((model, cid), {"score": 0.7, "latency": None})


async def refresh_channel(client, ch: dict) -> ChannelState:
    """拉取单个渠道的模型列表并更新健康状态"""
    cs = get_cs(ch["id"])
    t0 = time.time()
    try:
        ids = await providers.fetch_models(client, ch["base_url"], ch["api_key"])
        cs.models = ids
        cs.valid = True
        cs.error = None
        cs.latency_ms = int((time.time() - t0) * 1000)
    except Exception as e:
        cs.valid = False
        cs.error = str(e)[:200]
    cs.last_check = time.time()
    return cs


async def refresh_quotas(client, cfg: dict):
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        cs = get_cs(ch["id"])
        cs.quota_ts = time.time()
        q = await providers.check_quota(client, ch)
        if q is None:
            cs.quota, cs.quota_error = None, None
        elif q.get("kind") == "error":
            cs.quota_error = q.get("detail")
        else:
            cs.quota, cs.quota_error = q, None


def model_targets(model: str, cfg: dict) -> list:
    """别名展开：返回上游模型 ID 列表，组内做 failover"""
    ids = [model]
    for t in (cfg.get("aliases") or {}).get(model, []):
        if t and t not in ids:
            ids.append(t)
    return ids


# 能力档位 → 0~1 归一化分（用于加权综合分）
_CAP_SCORE = {3: 1.0, 2: 0.6, 1: 0.25}
# 策略权重 (智能, 稳定, 速度)：每种策略都考虑三项，只是侧重不同（借鉴 FreeLLMAPI 思路）
_STRATEGY_WEIGHTS = {
    "quality":    (0.60, 0.30, 0.10),   # 智能优先：能力为主，稳定其次，速度少量
    "balanced":   (0.35, 0.40, 0.25),   # 均衡：稳定/速度并重，能力兜底
    "stability":  (0.20, 0.70, 0.10),   # 稳定优先：成功率为主
    "speed":      (0.10, 0.25, 0.65),   # 速度优先：延迟为主
}
_CAP_BY_TIER = _CAP_SCORE
_SPEED_K = 400.0  # 400/(400+ms) 把延迟映射到 0~1（400ms 为 0.5）


def _model_composite(m: dict, strategy: str) -> float:
    """模型级综合分（用于 /v1/models 列表、模型视图排序，与 FE compScore 对齐）

    m 是 model_view() 输出的字典（含 tier / channels / status / test_reason / ...）。"""
    wcap, wstab, wspd = _STRATEGY_WEIGHTS.get(strategy, _STRATEGY_WEIGHTS["balanced"])
    cap = _CAP_BY_TIER.get(m.get("tier") or 2, 0.6)
    score, lat = 0.0, float("inf")
    for c in m.get("channels") or []:
        if c.get("score") and c["score"] > score:
            score = c["score"]
        if c.get("available") and c.get("latency_ms") and c["latency_ms"] < lat:
            lat = c["latency_ms"]
    score = score or 0.7
    speed = _SPEED_K / (_SPEED_K + lat) if lat < 1e8 else 0.0
    return wcap * cap + wstab * score + wspd * speed


import re as _re_ver  # 与 FE _lastVersion 对齐：剥 -\d+[bB]$ 取末位数字
_VER_RE_STRIP = _re_ver.compile(r"-\d+[bB](?=[^\d.]|$)")
_VER_RE_FIND = _re_ver.compile(r"(\d+(?:\.\d+)+|\d+)")
def _last_version(s: str):
    if not s:
        return None
    s = _VER_RE_STRIP.sub("", s)
    m = None
    val = None
    for m in _VER_RE_FIND.finditer(s):
        val = float(m.group(1))
    return val


def _composite(c, cfg, strategy: str) -> float:
    """综合评分 = w智能*能力档 + w稳定*实测成功率 + w速度*延迟分"""
    s = get_stat(c["model"], c["channel"]["id"])
    cs_lat = (channels.get(c["channel"]["id"]).latency_ms) or 9999
    lat = s["latency"] or cs_lat
    w_cap, w_stab, w_spd = _STRATEGY_WEIGHTS.get(strategy, _STRATEGY_WEIGHTS["balanced"])
    tier = capability.tier_of(c["model"], cfg.get("model_tiers"))
    cap = _CAP_BY_TIER.get(tier, 0.6)
    speed = _SPEED_K / (_SPEED_K + lat) if lat else 0.0
    return w_cap * cap + w_stab * s["score"] + w_spd * speed


def candidates_for(model: str, cfg: dict) -> list:
    """候选列表（含别名展开），按当前策略的综合分排序。
    返回 [{"channel": 渠道dict, "model": 上游模型ID}]"""
    now = time.time()
    out, seen = [], set()
    preempt = cfg.get("adaptive_preemption", True)

    def collect(ignore_cooldown: bool):
        for mid in model_targets(model, cfg):
            for ch in cfg["channels"]:
                if not ch.get("enabled", True):
                    continue
                cs = channels.get(ch["id"])
                if not cs or cs.valid is not True or mid not in cs.models:
                    continue
                key = (mid, ch["id"])
                if key in seen:
                    continue
                if not ignore_cooldown and cooldown.get(key, 0) > now:
                    continue
                # 429 自学预判：预计这条会越线 → 本轮跳过（仍有兜底逻辑）
                if not ignore_cooldown and preempt and throttle.blocked(ch["id"], mid):
                    continue
                seen.add(key)
                out.append({"channel": ch, "model": mid})

    collect(ignore_cooldown=False)
    if not out:
        collect(ignore_cooldown=True)  # 兜底：全被冷却/预判跳过也给出候选，避免直接 404

    strategy = cfg.get("route_strategy", "balanced")
    out.sort(key=lambda c: -_composite(c, cfg, strategy))
    return out


def model_view(cfg: dict) -> list:
    """模型视图：状态三档 —— ok(免费可调/绿) / limited(暂时限流或冷却/黄) / down(硬不可用/红)

    语义：暂时超过额度被限流的模型仍算「可用」范畴，只是标注受限；仅 402/403/下线 等
    硬失败才记为 down。"""
    now = time.time()
    agg = {}
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        cs = channels.get(ch["id"])
        if not cs:
            continue
        for m in cs.models:
            entry = agg.setdefault(m, [])
            in_cool = cooldown.get((m, ch["id"]), 0) > now
            ch_ok = bool(cs.valid) and not in_cool
            s = get_stat(m, ch["id"])
            entry.append({
                "channel_id": ch["id"],
                "channel_name": ch.get("name") or ch["type"],
                "available": ch_ok,
                "in_cooldown": in_cool,
                "latency_ms": cs.latency_ms,
                "score": s["score"],
            })
    models = []
    for m, chans in agg.items():
        ms = model_status.get(m)
        any_ok = any(c["available"] for c in chans)
        any_cool = any(c["in_cooldown"] for c in chans)
        tested = ms is not None
        reason = (ms.get("reason", "") if ms else "")
        ms_state = ms.get("state") if ms else None
        if ms is not None and (ms_state == "down"
                               or (ms_state is None and not ms.get("available"))):
            status = "down"          # 402/403/下线 等硬失败
            available = False
        elif any_ok:
            status = "ok"            # 免费且当前可调
            available = True
        elif any_cool or ms_state == "limited":
            status = "limited"       # 暂时限流/冷却中，仍算可用范畴
            available = False
        else:
            status = "down"
            available = False
        models.append({
            "id": m,
            "available": available,
            "status": status,
            "tested": tested,
            "test_reason": reason,
            "test_ts": (ms.get("ts", 0) if ms else 0),
            "any_channel_available": any_ok,
            "channel_count": len(chans),
            "tier": capability.tier_of(m, cfg.get("model_tiers")),
            "channels": chans,
        })
    models.sort(key=lambda x: (x["status"] != "ok", x["id"]))
    return models


def alias_view(cfg: dict) -> list:
    """别名列表视图：可用优先"""
    now = time.time()
    out = []
    for name, targets in (cfg.get("aliases") or {}).items():
        ok = False
        for t in targets:
            for ch in cfg["channels"]:
                cs = channels.get(ch["id"])
                if (ch.get("enabled", True) and cs and cs.valid
                        and t in cs.models and cooldown.get((t, ch["id"]), 0) <= now):
                    ok = True
                    break
            if ok:
                break
        out.append({"name": name, "targets": targets, "available": ok})
    out.sort(key=lambda a: (not a["available"], a["name"]))
    return out


def list_model_ids(cfg: dict) -> list:
    """对外 /v1/models 列表：别名在前，真实模型在后"""
    ids = [a["name"] for a in alias_view(cfg)]
    ids += [m["id"] for m in model_view(cfg)]
    return ids


# 客户端可用的「特殊模型名」：请求这些名字时由网关自动选最优真实模型，
# 并在请求失败时自动跨模型切换（用户感觉是「无感 failover」）。
RESERVED_AUTO = {
    "auto": "balanced",          # 默认 = 均衡（简短别名）
    "auto:quality": "quality",
    "auto:stability": "stability",
    "auto:speed": "speed",
}


def is_reserved_auto(model: str) -> bool:
    return model in RESERVED_AUTO


def auto_strategy_of(model: str) -> str:
    return RESERVED_AUTO.get(model, "balanced")


def candidates_for_auto(strategy: str, cfg: dict) -> list:
    """给「auto」请求生成候选：所有当前可用 (模型, 渠道) 组合。

    排序规则与 /v1/models 一致：收藏的模型优先（组内仍按策略综合分），
    然后才是未收藏模型。这样「列表里排前面的」就是「实际会被先用到的」——
    若用户收藏了某模型，auto 会先试它，失败才轮到其余模型。"""
    now = time.time()
    pinned = set(cfg.get("pinned") or [])
    out, seen = [], set()
    preempt = cfg.get("adaptive_preemption", True)
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        cs = channels.get(ch["id"])
        if not cs or not cs.valid:
            continue
        for m in cs.models:
            key = (m, ch["id"])
            if key in seen or cooldown.get(key, 0) > now:
                continue
            ms = model_status.get(m)
            if ms is not None and not ms.get("available"):
                continue
            if preempt and throttle.blocked(ch["id"], m):
                continue
            seen.add(key)
            out.append({"channel": ch, "model": m})

    # 收藏优先（0 < 1），组内按策略综合分从高到低
    out.sort(key=lambda c: (0 if c["model"] in pinned else 1,
                            -_composite(c, cfg, strategy)))
    return out


def list_reserved_auto() -> list:
    return list(RESERVED_AUTO.keys())


def channel_available_models(cid: str) -> int:
    """某渠道「当前可用」的模型数：不在冷却且未被模型级标记为不可用"""
    cs = channels.get(cid)
    if not cs:
        return 0
    now = time.time()
    avail = 0
    for m in cs.models:
        if cooldown.get((m, cid), 0) > now:
            continue
        ms = model_status.get(m)
        if ms is not None and not ms.get("available"):
            continue
        avail += 1
    return avail
