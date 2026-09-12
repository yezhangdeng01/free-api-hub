"""运行时状态：渠道健康、模型注册表、评分排序、错误分类冷却"""
import collections
import re
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
unverified: set = set()  # 冷却到期但还没实测确认恢复的 (模型, 渠道)
stats: dict = {}      # (上游模型ID, channel_id) -> {"score": 0~1, "latency": EMA毫秒}
model_status: dict = {}  # model_id -> {"available": bool, "reason": str, "ts": float, "channel": str}
channel_down: dict = {}  # (上游模型ID, channel_id) -> {"reason": str, "ts": float} 该渠道上此模型硬不可用
# 上游响应头里的官方限流信息（Groq/NIM 等返回 x-ratelimit-*）：(模型, 渠道) -> {"remaining": int, "reset_ts": float}
ratelimit: dict = {}
last_probe_ok: dict = {}  # (模型, 渠道) -> 上次探测成功的 ts（魔搭等按次计费平台用于省探测预算）

# ---- 渠道级 429 熔断（修魔搭等账号级限流：整个 Key 被限，所有模型一起 429）----
_CH429_WINDOW = 600    # 10 分钟窗口
_CH429_MIN_MODELS = 2  # 窗口内 ≥2 个不同模型撞 429 → 判定账号级限流
_CH429_COOL = 600      # 渠道冷却 10 分钟
_channel_429_events: dict = {}  # channel_id -> deque[(ts, model_id)]
channel_cool: dict = {}  # channel_id -> 冷却截止时间
channel_last_ok: dict = {}  # channel_id -> 最近一次成功请求的 ts（区分账号级/按模型限流）

# 渠道被拒（401/403）后，多久之内算「最近成功过」——用它区分账号级与模型级的权限问题
_AUTH_MODEL_LEVEL_WINDOW = 600.0


def channel_recently_ok(cid: str, within: float = _AUTH_MODEL_LEVEL_WINDOW) -> bool:
    """该渠道最近（默认 10 分钟内）有没有成功服务过请求。

    用来区分 401/403 的两种来源，**不要只看状态码**：
    - **账号级**（Key 失效 / 权限被撤）：所有模型都会失败，最近没有成功 → 该停用整个渠道；
    - **模型级**（这个模型要付费订阅 / 无权限 / 地区限制）：渠道其他模型还好好的 →
      只该标这一个模型，不能让一个模型的权限问题把整渠道（如 HF 的 139 个模型）
      一起停到下次健康检查。"""
    ts = channel_last_ok.get(cid)
    return bool(ts and time.time() - ts < within)


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
    unverified.difference_update(k for k in list(unverified) if k[1] not in ids)
    for key in list(stats):
        if key[1] not in ids:
            stats.pop(key, None)
    for key in list(channel_down):
        if key[1] not in ids:
            channel_down.pop(key, None)
    for key in list(ratelimit):
        if key[1] not in ids:
            ratelimit.pop(key, None)
    for key in list(last_probe_ok):
        if key[1] not in ids:
            last_probe_ok.pop(key, None)
    for cid in list(channel_cool):
        if cid not in ids:
            channel_cool.pop(cid, None)
            _channel_429_events.pop(cid, None)
    for cid in list(channel_last_ok):
        if cid not in ids:
            channel_last_ok.pop(cid, None)


def mark_channel_down(mid: str, cid: str, reason: str):
    """该渠道上的该模型硬不可用（402/403/404/400 等），按 (模型, 渠道) 记录，
    不再污染同名模型在其他渠道的状态。恢复路径：真实请求成功或手动测试。"""
    channel_down[(mid, cid)] = {"reason": (reason or "")[:200], "ts": time.time()}
    save_runtime_state()   # 关键硬失败证据即时落盘，不排队等节流，保证扫描结果重启不丢


def mark_channel_up(mid: str, cid: str):
    channel_down.pop((mid, cid), None)
    save_runtime_state()


# ---- 429 响应体分类：区分「每分钟限流」「免费额度用尽」「付费余额不足」----
# ⚠️ 光看正文分不出后两者：**魔搭的每日免费额度用完，正文就是 `insufficient balance`**
# （2026-09-12 实测：魔搭 429 → {"error":{"message":"insufficient balance"}}，翌日自动恢复；
#  OpenRouter 同样正文则是真没 credits，要充值）。所以必须结合渠道类型：
#  - 免费额度型渠道（modelscope/gemini/nim/agnes/huggingface）：额度类措辞一律当「等窗口刷新」；
#  - 其它（有余额接口的付费渠道）：余额/额度不足 → 需充值 → down，等不来自愈。
# 最权威的判据是平台余额接口（providers.check_quota），调用方查得到就覆盖这里的推断。
_FREE_QUOTA_TYPES = {"modelscope", "gemini", "nim", "agnes", "huggingface"}
_PAID_TEXT = ("insufficient balance", "insufficient_quota", "insufficient funds",
              "credit balance", "no credit", "recharge", "top up", "add credit",
              "purchase", "billing", "余额", "充值", "欠费", "资源包", "购买")
_FREE_TEXT = ("free tier", "freetier", "free-models-per-day", "免费额度",
              "daily", "per day", "day limit", "monthly",
              "今日", "每日", "当日", "已用完", "用尽", "quota exceeded", "exceeded your current quota")
_MINUTE_429 = ("per minute", "per-min", "rpm", "per hour", "too many requests",
               "too frequent", "rate limit", "每分钟", "频繁", "速率", "minute limit")


def _secs_to_tomorrow() -> int:
    """冷却到下一个 00:05（额度按天刷新，留 5 分钟缓冲）"""
    t = time.localtime()
    secs = (24 - t.tm_hour - 1) * 3600 + (60 - t.tm_min - 1) * 60 + (65 - t.tm_sec)
    return int(min(max(secs, 600), 86400))


def classify_429(body: str, ch_type: str = None, retry_after=None) -> tuple:
    """解析 429 响应体 → (类型, 建议冷却秒数, 可读说明)。四档：

    paid_balance → 冷却 0（调用方按 down 处理：等不来自愈，要充值/换 key）
    free_daily   → 冷却到明天凌晨（免费额度按天刷新，次日自动回来）
    minute       → 90 秒（上游给了 Retry-After 就用它）
    unknown      → 300 秒（保守；交给 429 自学水位接着调）

    上游若明确给了较短 Retry-After（≤10 分钟），说明它自己认为很快能好 →
    一律按分钟级处理，不按「明天」冷却。"""
    text = (body or "").lower()
    free_ch = (ch_type or "") in _FREE_QUOTA_TYPES
    paid = any(k in text for k in _PAID_TEXT)
    daily = any(k in text for k in _FREE_TEXT)
    if free_ch:
        if paid or daily:
            label, note = "free_daily", "免费额度用完，冷却至明天（次日自动恢复）"
        elif any(k in text for k in _MINUTE_429):
            label, note = "minute", "每分钟限流"
        else:
            label, note = "unknown", "暂时限流"
    else:
        if paid:
            label, note = "paid_balance", "账户余额/额度不足，需充值或换 key（硬不可用）"
        elif daily:
            label, note = "free_daily", "额度用尽，冷却至明天"
        elif any(k in text for k in _MINUTE_429):
            label, note = "minute", "每分钟限流"
        else:
            label, note = "unknown", "暂时限流"
    # 上游给的 Retry-After 若很短，说明不是「额度到明天」那种限制
    if label in ("free_daily", "unknown") and retry_after and 0 < retry_after <= 600:
        label, note = "minute", note + f"（上游 Retry-After {retry_after}s）"
    if label == "paid_balance":
        return (label, 0, note)
    if label == "free_daily":
        return (label, _secs_to_tomorrow(), note)
    if label == "minute":
        return (label, max(90, int(retry_after or 0)), note)
    return (label, 300, note)


# 永久不可用判定：这些错误「冷却到明天」也不会自愈，应记 down 而非 limited
_PERMANENT_KEYWORDS = (
    "余额", "充值", "资源包", "已用完", "用尽", "购买", "balance", "credit",
    "recharge", "top up", "purchase", "insufficient", "not have sufficient",
    "never purchase", "only available", "非免费", "所有提供方均收费",
)


# ---- 地域封锁：本机出口网络的问题，不是模型的错 ----
_GEO_HINTS = ("user location is not supported", "location is not supported",
              "not available in your country", "not available in your region",
              "unsupported_country", "region not supported", "failed_precondition",
              "地域", "地区不支持", "所在地区", "不支持您所在的")


def is_geo_block(text: str) -> bool:
    """上游以「你所在地区不支持」拒了请求（Google 系常见——走代理时尤其频繁）。

    2026-09-12 实测：同一批请求里有的 200 答对、有的 400 FAILED_PRECONDITION
    `User location is not supported for the API use`。这**既不是模型不稳，也不是模型下线**，
    是本机出口网络的问题；所以不写稳定分、不标 down、不冷却（与本地 DNS 同等对待）。"""
    t = (text or "").lower()
    return any(k in t for k in _GEO_HINTS)


def kind_from_error(err: str) -> str:
    """从历史 error 文本反推失败类型（口径与实时路径的 kind 一致，供回填用）。

    实时路径有真实 kind，不需要猜；只有回填 usage.db 老数据时才走这里。"""
    t = (err or "").lower()
    if is_geo_block(t):
        return "geo"
    if "getaddrinfo" in t or "name resolution" in t or "proxy" in t:
        return "local_net"
    if ("客户端断开" in t or "客户端中断" in t or "client disconnect" in t
            or "cancelled" in t or "canceled" in t):
        return "cancelled"      # 消费方断开且没拿到结束标记，上游没错（见 _STAB_SKIP_KINDS）
    if "429" in t or "rate limit" in t or "限流" in t or "频繁" in t:
        return "rate_limit"
    if "insufficient" in t or "balance" in t or "余额" in t or "额度" in t or "欠费" in t:
        return "balance"
    if re.search(r"http\s*[45]\d\d", t) and "http 5" not in t:
        return "client"
    return "connect"


def backfill_stab(rows) -> int:
    """用历史**真实调用**回填稳定分窗口（rows 来自 store.recent_outcomes，按新→旧）。

    为什么需要：稳定分只认真实调用，而历史真实调用全在 usage.db 里；旧口径（探测与真实
    混算的 score/n）已按新口径丢弃，不回填的话所有模型从空窗口起步，「稳定优先」要等很久
    才有效。只填**还没有窗口**的 (模型,渠道)，实时攒下的样本绝不被覆盖。
    失败按 kind_from_error 分类：429/余额/4xx/本地网络/地域/客户端中断 一律跳过。返回回填条数。"""
    per = {}
    for row in rows:
        model, cid, success, err = row[0], row[1], row[2], row[3]
        # 第 6 位是 cancelled（老库/老调用可能没有这一列）
        if len(row) > 5 and row[5]:
            continue
        lst = per.setdefault((model, cid), [])
        if len(lst) >= STAB_WIN:
            continue
        if success:
            lst.append(1)
        elif kind_from_error(err) not in _STAB_SKIP_KINDS:
            lst.append(0)
    n = 0
    for (model, cid), lst in per.items():
        if not lst or (stats.get((model, cid)) or {}).get("win"):
            continue
        s = stats.setdefault((model, cid), {"win": [], "latency": None, "ttft": None})
        s["win"] = list(reversed(lst))       # 与新→旧相反，存成旧的在前（与实时追加一致）
        n += 1
    if n:
        save_runtime_state()
    return n


def is_permanent_failure(status: int, text: str = "") -> bool:
    """HTTP 状态/响应体是否为「永久不可用」（down），而非「暂时受限」（limited）。

    - 400/402/403/404/405：付费/权限/参数/已下线 → 等也不会恢复，记 down。
    - 429 且正文含「余额/充值/购买」等：额度用尽需人工充值 → 记 down。
    - 地域封锁（400 FAILED_PRECONDITION）：本机网络问题，不是下游结论 → 不算永久失败。
    - 其余 429（每分钟限流）/5xx/连接失败 → 暂时受限，记 limited。"""
    if is_geo_block(text):
        return False
    if status in (400, 402, 403, 404, 405):
        return True
    if status == 429:
        t = (text or "").lower()
        return any(k in t for k in _PERMANENT_KEYWORDS)
    return False


# ---- 上游 x-ratelimit-* 响应头（Groq/NIM 等）：官方给的剩余额度，比自学水位精确 ----
def _parse_duration(s: str) -> float:
    """解析 Groq 风格时长 "2m39.5s" / "1h" / "45s"，失败返回 0"""
    import re
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$", (s or "").strip())
    if not m or not any(m.groups()):
        return 0.0
    h, mi, sec = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


def note_ratelimit_headers(cid: str, mid: str, headers) -> bool:
    """从响应头读官方限流信息并记录。返回 True 表示本次记录到有效数据。"""
    try:
        remaining = headers.get("x-ratelimit-remaining-requests")
        if remaining is None:
            remaining = headers.get("x-ratelimit-remaining")
        if remaining is None:
            return False
        reset_raw = (headers.get("x-ratelimit-reset-requests")
                     or headers.get("x-ratelimit-reset") or "")
        secs = _parse_duration(reset_raw)
        ratelimit[(mid, cid)] = {
            "remaining": int(float(remaining)),
            "reset_ts": time.time() + secs if secs else 0,
        }
        return True
    except (TypeError, ValueError):
        return False


def ratelimit_exhausted(mid: str, cid: str, now: float = None) -> bool:
    """官方头显示剩余请求数为 0 且还没到重置时间 → 路由前直接跳过"""
    st = ratelimit.get((mid, cid))
    if not st or st["remaining"] > 0:
        return False
    now = now if now is not None else time.time()
    if st["reset_ts"] and st["reset_ts"] <= now:
        ratelimit.pop((mid, cid), None)  # 已重置，解除
        return False
    return True


def note_channel_429(cid: str, mid: str, retry_after: int = None):
    """渠道级 429 计数：窗口内 ≥2 个不同模型撞 429 → 判定账号级限流，整个渠道进冷却。
    这样魔搭这类按 Key 限流的平台被限后，UI 会显示受限而不是全绿。
    例外：渠道 2 分钟内刚有成功请求 → 更可能是"按模型限流"（Gemini 等免费档每个模型
    有独立 RPM），账号级限流会让成功请求也一起停，所以有成功就不熔断。"""
    now = time.time()
    last_ok = channel_last_ok.get(cid)
    if last_ok and now - last_ok < 120:
        return False
    dq = _channel_429_events.setdefault(cid, collections.deque(maxlen=500))
    dq.append((now, mid))
    while dq and dq[0][0] < now - _CH429_WINDOW:
        dq.popleft()
    distinct = {m for _, m in dq}
    if len(distinct) >= _CH429_MIN_MODELS:
        seconds = max(_CH429_COOL, min(retry_after or 0, 1800))
        channel_cool[cid] = now + seconds
        return True
    return False


def channel_cooling(cid: str, now: float = None) -> bool:
    now = now if now is not None else time.time()
    return channel_cool.get(cid, 0) > now


def mark_channel_quota_exhausted(cid: str, reason: str = ""):
    """账户级「当天额度用完」：整个渠道冷却到明天 00:05，无需等第二个模型撞 429。

    魔搭等按「每日调用次数」限额的平台，一旦触发 daily 型 429，说明当天免费额度已用尽，
    该 Key 下**所有**模型都会跟着 429（账户级，不是按模型级）。普通按模型 RPM 限流的平台
    （Gemini/NIM 等）每个模型有独立额度，不会走到这里（它们的 429 是 minute 型）。
    只有 `classify_429` 判为 `daily`（响应体含「今日/每日/额度已用完」等）才调用本函数。"""
    # 冷却到下一个 00:05（额度按天刷新，留 5 分钟缓冲）
    now = time.time()
    t = time.localtime(now)
    secs = (24 - t.tm_hour - 1) * 3600 + (60 - t.tm_min - 1) * 60 + (65 - t.tm_sec)
    channel_cool[cid] = now + min(max(secs, 600), 86400)
    save_runtime_state()


def mark_model_status(model: str, available: bool, reason: str = "", channel: str = "",
                      state: str = None):
    """记一次模型级测试/调用的真实结果并持久化。

    state: 'ok' 免费可调 / 'limited' 暂时受限（429/5xx/连接，冷却后会恢复）/ 'down' 硬不可用。
    available 由 state 唯一决定：只有 'ok' 才可路由。'limited'（受限）也**不可路由**——
    否则「余额不足/限流」的模型会冒充可用，一用就报错。"""
    if state is None:
        state = "ok" if available else "down"
    model_status[model] = {"available": state == "ok", "state": state,
                           "reason": (reason or "")[:200],
                           "ts": time.time(), "channel": channel}
    try:
        from . import store
        store.persist_model_status(model, model_status[model])
    except Exception:
        pass  # 持久化失败不影响内存状态


def restore_model_status():
    """启动时从磁盘恢复已测状态，并修正旧数据的 available 语义。

    历史 bug：旧版本把 'limited' 的 available 也写成 True，导致受限模型重启后冒充可用。
    这里按 state 重新推导 available，并把「余额不足 / 402/403/404 / 非免费」这类永久不可用
    从 limited 归正为 down，无需手动清库。归正后的正确结果会一次性回写磁盘。"""
    from . import store
    # 复用永久不可用关键词，另加状态码串（reason 里是 "HTTP 402: ..." 这种文本）
    _PERM_REASONS = _PERMANENT_KEYWORDS + ("402", "403", "404", "405")
    dirty = False
    raw = store.load_model_status()
    # DEBUG: print dirty decision process
    for mid, entry in raw.items():
        if not isinstance(entry, dict) or "available" not in entry:
            continue
        entry = dict(entry)
        st = entry.get("state")
        reason = (entry.get("reason") or "").lower()
        if st == "limited" and any(k in reason for k in _PERM_REASONS):
            st = "down"
        st = st or ("ok" if entry.get("available") else "down")
        new_avail = (st == "ok")
        if entry.get("state") != st or entry.get("available") != new_avail:
            dirty = True
        entry["state"] = st
        entry["available"] = new_avail
        model_status[mid] = entry
    # DEBUG
    if dirty:
        import logging
        logging.getLogger("api-hub").info(f"restore_model_status: 将回写磁盘，脏数据条数待统计")
    if dirty:
        # 一次性迁移：把修正后的正确状态回写磁盘，避免旧脏数据被别处读盘再次误用
        try:
            store._atomic_write(store.MODEL_STATUS_PATH, model_status)
        except Exception:
            import logging
            logging.getLogger("api-hub").warning("归正后的模型状态回写失败，不影响内存态")


# ---- 运行时状态持久化：冷却/待验证/渠道级硬失败落盘，重启不丢扫描结果 ----
_save_ts = {"v": 0.0}
_save_dirty = {"v": False}


def save_runtime_state():
    from . import store
    now = time.time()
    try:
        store.persist_runtime_state({
            "saved_at": round(now, 1),
            "cooldown": {f"{k[0]}|{k[1]}": round(v, 1) for k, v in cooldown.items() if v > now},
            "unverified": [f"{k[0]}|{k[1]}" for k in unverified],
            "channel_down": {f"{k[0]}|{k[1]}": v for k, v in channel_down.items()},
            "ratelimit": {f"{k[0]}|{k[1]}": v for k, v in ratelimit.items()},
            "channel_cool": {cid: round(v, 1) for cid, v in channel_cool.items() if v > now},
            "throttle": throttle.snapshot(),
            # 渠道评分（稳定分窗口/延迟/首字节）：只落「有真实调用样本或有延迟」的条目，
            # 否则模型×渠道全量落盘太大。win 存最近 N 次真实调用的 1/0，跨重启继续累计。
            "stats": {f"{k[0]}|{k[1]}": {c: s.get(c) for c in _STAB_COLS}
                      for k, s in stats.items()
                      if (s.get("win") or s.get("latency") or s.get("ttft"))},
        })
    except Exception:
        pass  # 持久化失败不影响内存状态


def restore_runtime_state():
    """启动时恢复上一轮扫描/请求留下的失败与冷却记录"""
    from . import store
    data = store.load_runtime_state()
    now = time.time()
    n_cool = 0
    for k, v in (data.get("cooldown") or {}).items():
        if v > now:
            mid, _, cid = k.partition("|")
            if mid and cid:
                cooldown[(mid, cid)] = v
                n_cool += 1
    for k in data.get("unverified") or []:
        mid, _, cid = k.partition("|")
        if mid and cid:
            unverified.add((mid, cid))
    for k, v in (data.get("channel_down") or {}).items():
        mid, _, cid = k.partition("|")
        if mid and cid:
            channel_down[(mid, cid)] = v
    for k, v in (data.get("ratelimit") or {}).items():
        mid, _, cid = k.partition("|")
        if mid and cid and isinstance(v, dict) and "remaining" in v:
            ratelimit[(mid, cid)] = v
    # 恢复渠道级冷却（含「账户级当天额度用完」的冷却到明天）
    for cid, v in (data.get("channel_cool") or {}).items():
        if v > now:
            channel_cool[cid] = v
    # 恢复 429 自学水位（throttle），并给仍在有效期内的 (渠道,模型) 一个保守短冷却，
    # 避免重启后受限模型立刻回绿、再次集中撞限（这是 Gemini/NIM 重启变绿的直接原因）
    throttle.restore(data.get("throttle") or {})
    # 恢复渠道评分（稳定分窗口/延迟/首字节）：这是「稳定优先」的历史依据，不恢复的话
    # 每次重启稳定分都从头开始。**旧版数据（只有 score/n，没有 win）直接丢弃**——旧 score 是
    # 探测与真实调用混算的，口径不同；真实历史用 scripts/backfill_stab.py 从 usage.db 回填。
    n_stat = 0
    for k, v in (data.get("stats") or {}).items():
        mid, _, cid = k.partition("|")
        if not (mid and cid and isinstance(v, dict)):
            continue
        try:
            win = [1 if int(x) else 0 for x in (v.get("win") or [])][-STAB_WIN:]
            stats[(mid, cid)] = {
                "win": win,
                "latency": int(v["latency"]) if v.get("latency") else None,
                "ttft": int(v["ttft"]) if v.get("ttft") else None,
            }
        except (TypeError, ValueError):
            continue
        n_stat += 1
    n_seeded = 0
    for cid, mid in throttle.learned_pairs():
        key = (mid, cid)
        if cooldown.get(key, 0) <= now:
            cooldown[key] = now + 300  # 与 rate_limit 默认冷却一致
            n_seeded += 1
    import logging
    logging.getLogger("api-hub").info(
        "已恢复运行时状态：冷却 %d，待验证 %d，渠道级硬失败 %d，限流头 %d，429 预判 %d（seed 冷却 %d，评分样本 %d）",
        n_cool, len(unverified), len(channel_down), len(ratelimit),
        len(throttle.learned_pairs()), n_seeded, n_stat)


def _request_save():
    """落盘请求：节流合并写盘，扫描几百个模型时不会疯狂写盘；
    关键状态（channel_down）已走 mark_channel_down 即时落盘，不受此处节流影响。"""
    _save_dirty["v"] = True
    if time.time() - _save_ts["v"] >= 1:  # 3s → 1s，缩小冷却/待验证状态的丢失窗口
        _save_ts["v"] = time.time()
        _save_dirty["v"] = False
        save_runtime_state()


def flush_runtime_state():
    """后台循环定期调用：把挂起的脏状态落盘"""
    if _save_dirty["v"]:
        _save_dirty["v"] = False
        _save_ts["v"] = time.time()
        save_runtime_state()


def get_model_status(model: str):
 return model_status.get(model)


def _update_score(model: str, cid: str, ok: bool, latency_ms=None, kind: str = None,
                  ttft_ms=None, source: str = "probe"):
    """记录一次调用的结果。

    只有 `source="real"`（真实 /v1 请求）才写稳定分窗口——主动探测/测试按钮的 1-token
    ping 与真实可用性无关。窗口只收「技术性失败」：上游超时/断连/断流/5xx/响应坏了；
    429·额度、4xx 权限下线、本地 DNS 一律不进（不是模型不稳，或根本不是上游的错）。
    延迟/首字节同样只认真实请求；没真实数据的模型由 _composite 回退渠道健康检查延迟。"""
    s = stats.setdefault((model, cid), {"win": [], "latency": None, "ttft": None})
    if source != "real":
        return
    # 失败**默认计入**（保守：未知失败就是不稳），只有下面这些明确「不是模型不稳」的才跳过
    if ok or kind not in _STAB_SKIP_KINDS:
        win = list(s.get("win") or [])
        win.append(1 if ok else 0)
        s["win"] = win[-STAB_WIN:]
    if ok and latency_ms:
        s["latency"] = latency_ms if s.get("latency") is None else int(s["latency"] * 0.7 + latency_ms * 0.3)
    if ok and ttft_ms:
        s["ttft"] = ttft_ms if s.get("ttft") is None else int(s["ttft"] * 0.7 + ttft_ms * 0.3)


def mark_ttft(model: str, cid: str, ttft_ms: int):
    """记录一次**真实流式请求**的首字节时间（TTFT）。速度分优先用它——它才反映「吐字快不快」，
    而渠道健康检查的延迟只是「列模型接口快不快」。"""
    if not ttft_ms:
        return
    s = stats.setdefault((model, cid), {"win": [], "latency": None, "ttft": None})
    s["ttft"] = ttft_ms if s.get("ttft") is None else int(s["ttft"] * 0.7 + ttft_ms * 0.3)
    _request_save()


def mark_result(model: str, cid: str, ok: bool, latency_ms=None,
                kind: str = None, retry_after: int = None, cooldown_seconds: int = None,
                source: str = "probe"):
    """记录一次调用结果：成功解除冷却，失败按类型冷却。

    `source` 决定要不要动稳定分：只有 `"real"`（真实 /v1 请求）会写稳定分窗口与延迟，
    `"probe"`（主动探测 / 模型测试 / 健康检查）只更新可用性状态（冷却、熔断、状态标记）。
    默认 `"probe"` 是安全默认：以后新增调用点忘了传，也不会污染稳定分。
    cooldown_seconds 由 429 响应体分类给出，优先级最高；其次是上游 Retry-After 头。"""
    _update_score(model, cid, ok, latency_ms, kind=kind, source=source)
    key = (model, cid)
    if ok:
        cooldown.pop(key, None)
        unverified.discard(key)      # 实测成功 → 恢复状态被确认
        channel_down.pop(key, None)  # 之前硬失败的 (模型,渠道) 也随之恢复
        channel_last_ok[cid] = time.time()
        if cid in channel_cool:
            # 成功调用 = 渠道确实可用（额度可能已刷新）→ 解除渠道级冷却并即时落盘，
            # 避免「手动测试成功但冷却到明天的状态还在、重启后渠道仍被封」。
            # 自动探测/路由不会在渠道冷却期间发请求，只有用户手动测试能走到这里。
            channel_cool.pop(cid, None)
            _channel_429_events.pop(cid, None)  # 清掉熔断计数，恢复后不再被旧账立刻熔断
            save_runtime_state()
    else:
        seconds = COOLDOWN_SECONDS.get(kind, DEFAULT_COOLDOWN)
        if retry_after and retry_after > 0 and not cooldown_seconds:
            seconds = max(seconds, min(retry_after, 3600))
        if cooldown_seconds:
            seconds = cooldown_seconds
        if seconds:
            cooldown[key] = time.time() + seconds
        unverified.add(key)  # 冷却到期后仍显示受限，直到实测/探测成功才转绿
    _request_save()


def get_stat(model: str, cid: str) -> dict:
    return stats.get((model, cid), {"win": [], "latency": None, "ttft": None})


_SCORE_PRIOR = 0.7      # 未知模型的先验分（中性：不奖不罚）
STAB_WIN = 10           # 稳定分只看最近 N 次**真实调用**
_STAB_K = 2.0           # 小样本收缩强度：n=2 时只用一半权重（旧版是 3）
_STAB_COLS = ("win", "latency", "ttft")
# 不算「模型不稳」的失败类型（时间/充值/权限能解决，或根本不是上游的错）：
#   rate_limit/balance/quota = 429 系列（冷却或充值就好，稳定分不该被扣）
#   client/auth = 4xx（模型在该渠道下线/无权限 → 走「硬不可用」，不是质量信号）
#   local_net  = 本地 DNS/代理问题（你自己的网络抖了）
#   geo        = 上游地域封锁（也是出口网络问题，见 is_geo_block）
#   cancelled  = 客户端主动断开（用户点停止 / 客户端超时），上游返回是正常的
_STAB_SKIP_KINDS = {"rate_limit", "balance", "quota", "client", "auth", "local_net", "geo",
                    "cancelled"}


def stab_of(s: dict):
    """稳定分 = 最近 N 次**真实调用**的成功率（向先验 0.7 收缩）；**没有真实样本 → None**。

    - 只认真实请求的结果：主动探测/测试按钮的 1-token ping 与真实可用性无关（免费额度、
      长上下文、流式都可能翻车），它们只更新可用性状态，不写这张窗口。
    - 只认「技术性失败」（上游超时/断连/断流/5xx/响应坏了）：429/额度、4xx 权限下线、
      本地 DNS 一律不进窗口——前者等窗口刷新或充值就恢复，后者根本不是上游的错。
    - 返回 None 表示「没数据」：加权策略里该维度不参与、权重归一给其它维度
      （不让一个假的 0.7 先验干扰排序），一旦有真实数据立刻以完整权重生效。
    """
    win = s.get("win") or []
    if not win:
        return None
    raw = sum(win) / len(win)
    conf = len(win) / (len(win) + _STAB_K)
    return round(_SCORE_PRIOR + (raw - _SCORE_PRIOR) * conf, 4)


def stab_raw(s: dict):
    """窗口原始成功率（给人看的「成功率 X%」，不做收缩）；没数据 → None"""
    win = s.get("win") or []
    return round(sum(win) / len(win), 3) if win else None


# ---------------- 模型 ID 命名变更 → 旧状态迁移 ----------------
# OpenRouter 2025 年起把模型 ID 从 `Org/Model` 规范为 `vendor/model`（全小写）。
# 已测的 down/limited 状态若还挂在旧命名上，重启 refresh 后会因旧 ID 不在新列表
# 而失联，新名模型 test=false 默认可用 → 「不可用模型重启变绿」。
# 这里在刷新拿到新列表时做「确定等价」的迁移，避免把不同模型错配。
_ORG_VENDOR = {   # OpenRouter 旧 Org 前缀 → 新 vendor 前缀（已知确定等价）
    "coherelabs": "cohere",
    "minimaxai": "minimax",
}


def _model_key(mid: str) -> str:
    """归一化模型 ID 用于跨命名匹配：小写、Org→vendor 前缀映射、分隔符统一为 '-'。"""
    import re as _re
    s = (mid or "").lower().strip()
    prefix = s.split("/")[0]
    if prefix in _ORG_VENDOR:
        s = _ORG_VENDOR[prefix] + s[len(prefix):]
    s = s.replace(" ", "-")
    s = _re.sub(r"[:/_.]+", "-", s)
    return s


def _migrate_named_models(ch: dict, old_models: list, new_models: list):
    """渠道模型列表刷新后，把旧命名的 down/limited 状态迁移到新命名。

    仅当旧 ID 归一化 key 与某个新 ID 归一化 key 完全一致才迁移（含 Org→vendor
    前缀映射），避免错配。仅对 openrouter 启用（其余渠道模型 ID 稳定）。"""
    if ch.get("type") != "openrouter" or not old_models:
        return
    new_set = set(new_models)
    # 新列表里每个归一化 key → 新 ID；key 重复则放弃该 key（歧义不迁移）
    key_to_new = {}
    for n in new_models:
        k = _model_key(n)
        key_to_new[k] = None if k in key_to_new else n
    cid = ch["id"]
    migrated = 0
    for old_mid in list(model_status.keys()):
        if old_mid in new_set:
            continue
        nk = key_to_new.get(_model_key(old_mid))
        if nk:
            model_status[nk] = model_status.pop(old_mid)
            migrated += 1
    for key in list(channel_down.keys()):
        mid, kcid = key
        if kcid != cid or mid in new_set:
            continue
        nk = key_to_new.get(_model_key(mid))
        if nk:
            channel_down[(nk, cid)] = channel_down.pop(key)
            migrated += 1
    if migrated:
        import logging
        logging.getLogger("api-hub").info(
            "渠道[%s] 模型命名变更，已迁移 %d 条旧状态到新命名", ch.get("name"), migrated)


async def refresh_channel(client, ch: dict) -> ChannelState:
    """拉取单个渠道的模型列表并更新健康状态"""
    cs = get_cs(ch["id"])
    t0 = time.time()
    try:
        ids = await providers.fetch_models(client, ch["base_url"], ch["api_key"])
        _migrate_named_models(ch, cs.models, ids)  # 命名变更 → 旧 down/limited 迁移到新名
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


# 三个维度都归一化到 0~1：cap 智能（AA 榜分归一化）/ stab 稳定（带样本量置信度）/ spd 速度
# 策略 = **主维度优先，按容忍带宽分档**（同档视为「差不多」）→ 档内再按另外两维加权。
#   为什么不用「严格主键排序」：主维度是连续分，几乎永不相等（AA 34.5 vs 34.6 就是不同），
#   那样等于只看主键、另两维形同虚设；
#   为什么不用「大权重加权」：权重再大也压不住主维度的极端值（1 分能力差 vs 稳定性崩掉）。
# band 就是「差不多」的宽度：能力带 0.10 ≈ AA 3.5 分（观测分布 7.8~42.3 归一化后）。
_STRATEGY_SPEC = {
    # 主维度 / 容忍带宽 / 档内加权（另两维，按 cap-stab-spd 去掉主维后的顺序）
    "quality":   {"primary": "cap",  "band": 0.10, "tie": (0.60, 0.40)},  # 智能优先：能力接近时看稳定6/速度4
    "stability": {"primary": "stab", "band": 0.08, "tie": (0.60, 0.40)},  # 稳定优先：稳定接近时看智能6/速度4
    "speed":     {"primary": "spd",  "band": 0.10, "tie": (0.55, 0.45)},  # 速度优先：速度接近时看智能5.5/稳定4.5
    "vision":    {"primary": "cap",  "band": 0.10, "tie": (0.60, 0.40)},  # 视觉：只看能看图的模型，能力优先+均衡
    "balanced":  {"weights": (0.35, 0.40, 0.25)},                         # 均衡：三维直接加权
}
_DIM_IDX = {"cap": 0, "stab": 1, "spd": 2}
_CAP_BY_TIER = capability._TIER_ANCHOR   # 手造 dict / 无榜分时的兜底锚点
_SPEED_K = 400.0  # 400/(400+ms) 把延迟映射到 0~1（400ms 为 0.5）


def _score_dims(dims: tuple, strategy: str) -> float:
    """把三维分按策略折成一个可排序的标量。

    带主维度的策略返回 `档号 + 档内加权`（档内加权 ∈ [0,1)），所以排序天然是
    「先按主维度分档、同档再按另外两维」——标量接口不变，前端 / 路由 / /v1/models
    共用同一套口径。

    `dims[1]`（稳定）**可能是 None** = 该 (模型,渠道) 还没有真实调用样本：
    - 加权策略（均衡）：把稳定维剔除、其余维度权重归一 —— 不让一个假的 0.7 先验干扰排序，
      等有真实数据再以完整权重生效；
    - 主维度=稳定的策略（稳定优先）：按先验 0.7 当「中性」处理，否则数据少时它几乎没有候选。
    """
    spec = _STRATEGY_SPEC.get(strategy) or _STRATEGY_SPEC["balanced"]
    if "weights" in spec:
        w = spec["weights"]
        if dims[1] is None:
            return (w[0] * dims[0] + w[2] * dims[2]) / (w[0] + w[2])
        return w[0] * dims[0] + w[1] * dims[1] + w[2] * dims[2]
    i = _DIM_IDX[spec["primary"]]
    others = [x for x in (0, 1, 2) if x != i]
    tie = sum(spec["tie"][n] * (dims[o] if dims[o] is not None else _SCORE_PRIOR)
              for n, o in enumerate(others))
    primary = dims[i] if dims[i] is not None else _SCORE_PRIOR
    return int(primary / spec["band"]) + tie


def _model_composite(m: dict, strategy: str) -> float:
    """模型级综合分 = 该模型**最优渠道**的综合分（用于 /v1/models 排序）

    与 candidates_for 的候选排序（_composite 单渠道版）同口径——这样「/v1/models 里
    排前面的」就是「auto 实际会先用到的」。旧版把「稳定分取最好的渠道、延迟取最快的
    渠道」分开取值，会拼出一个任何一次请求都拿不到的组合。"""
    cap = m.get("cap_score")
    if cap is None:
        cap = _CAP_BY_TIER.get(m.get("tier") or 2, _CAP_BY_TIER[2])
    chans = m.get("channels") or []
    pool = [c for c in chans if c.get("available")] or chans
    best = -1.0
    for c in pool:
        lat = c.get("latency_ms")
        speed = _SPEED_K / (_SPEED_K + lat) if lat else 0.0
        stab = c.get("stab")            # None = 该渠道没有真实调用样本（不参与加权）
        cand = _score_dims((cap, stab, speed), strategy)
        if cand > best:
            best = cand
    return max(best, 0.0)


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
    """候选（模型×渠道）综合分：按当前策略把三维分折算成标量。

    智能 = 连续能力分（AA 榜分归一化，无榜分用档位锚点）；稳定 = 近 N 次**真实调用**成功率
    （没有真实样本 → None，加权时该维剔除）；速度 = 首字节(TTFT) 优先 → 实测总延迟 →
    渠道健康检查延迟。"""
    s = get_stat(c["model"], c["channel"]["id"])
    cs_lat = (channels.get(c["channel"]["id"]).latency_ms) or 9999
    lat = s.get("ttft") or s["latency"] or cs_lat
    tier = capability.tier_of(c["model"], cfg.get("model_tiers"))
    cap = capability.capability_score(c["model"], tier, cfg.get("model_tiers"))
    speed = _SPEED_K / (_SPEED_K + lat) if lat else 0.0
    return _score_dims((cap, stab_of(s), speed), strategy)


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
                if key in seen or key in channel_down:
                    continue
                # 官方 x-ratelimit 头显示额度耗尽且未重置 → 本轮跳过
                if ratelimit_exhausted(mid, ch["id"], now):
                    continue
                # 渠道级熔断（账号级限流）：整条渠道先跳过
                if channel_cooling(ch["id"], now):
                    continue
                if not ignore_cooldown:
                    if cooldown.get(key, 0) > now:
                        continue
                    # 429 自学预判：预计这条会越线 → 本轮跳过（仍有兜底逻辑）
                    if preempt and throttle.blocked(ch["id"], mid):
                        continue
                seen.add(key)
                out.append({"channel": ch, "model": mid})

    collect(ignore_cooldown=False)
    if not out:
        collect(ignore_cooldown=True)  # 兜底：全被冷却/预判跳过也给出候选，避免直接 404

    strategy = cfg.get("route_strategy", "balanced")
    out.sort(key=lambda c: -_composite(c, cfg, strategy))
    return out


def candidates_for_test(model: str, cfg: dict) -> list:
    """手动测试专用候选（`/api/models/test` 用户点按钮）：**绕过所有冷却**。

    渠道级冷却（channel_cool，如魔搭 free_daily → 明天）、模型级冷却、待验证(unverified)、
    429 自学预判(throttle.blocked) 一律不挡——用户主动点测试 = 明确承担额度消耗，
    而且测试成功应立即可恢复（见 mark_result 成功分支解除 channel_cool）。
    自动探测 / 真实路由仍走 candidates_for / probe 逻辑，继续尊重冷却，不受影响。

    仍排除：渠道未启用 / 健康检查未通过 / 模型不在该渠道 / 该渠道上该模型硬失败
    (channel_down) / 官方限流头显示额度耗尽未重置(ratelimit_exhausted)。
    返回结构与 candidates_for 一致，按当前策略综合分排序（正常渠道仍排前面先试）。"""
    now = time.time()
    out, seen = [], set()
    for mid in model_targets(model, cfg):
        for ch in cfg["channels"]:
            if not ch.get("enabled", True):
                continue
            cs = channels.get(ch["id"])
            if not cs or cs.valid is not True or mid not in cs.models:
                continue
            key = (mid, ch["id"])
            if key in seen or key in channel_down:
                continue
            if ratelimit_exhausted(mid, ch["id"], now):
                continue
            seen.add(key)
            out.append({"channel": ch, "model": mid})
    strategy = cfg.get("route_strategy", "balanced")
    out.sort(key=lambda c: -_composite(c, cfg, strategy))
    return out


def model_view(cfg: dict) -> list:
    """模型视图：状态三档 —— ok(免费可调/绿) / limited(暂时限流或冷却/黄) / down(硬不可用/红)

    语义：暂时超过额度被限流的模型仍算「可用」范畴，只是标注受限；仅 402/403/下线 等
    硬失败才记为 down，且按 (模型, 渠道) 粒度记录——只有所有渠道都硬失败才算整个模型 down。
    冷却到期但未实测确认（unverified）、429 预判跳过（preempted）、渠道级熔断（账号级限流）
    都会让该渠道显示受限，避免「界面绿色但实际用不了」。"""
    now = time.time()
    agg = {}
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        cs = channels.get(ch["id"])
        if not cs:
            continue
        ch_cool = channel_cooling(ch["id"], now)
        preempt = cfg.get("adaptive_preemption", True)
        for m in cs.models:
            key = (m, ch["id"])
            entry = agg.setdefault(m, [])
            in_cool = cooldown.get(key, 0) > now or key in unverified
            ch_down = key in channel_down
            preempted = bool(preempt and not in_cool and not ch_down
                             and not ch_cool and throttle.blocked(ch["id"], m))
            ms_m = model_status.get(m)
            model_down = ms_m is not None and not ms_m.get("available")
            s = get_stat(m, ch["id"])
            entry.append({
                "channel_id": ch["id"],
                "channel_name": ch.get("name") or ch["type"],
                "available": bool(cs.valid) and not ch_cool and not ch_down
                             and not in_cool and not preempted and not model_down,
                "in_cooldown": in_cool or ch_cool,
                "down": ch_down,
                "preempted": preempted,
                # 响应速度：首字节(TTFT) 优先 → 实测总延迟 → 渠道健康检查延迟
                # ——与 _composite / _model_composite / FE compScore 同一口径
                "latency_ms": s.get("ttft") or s["latency"] or cs.latency_ms,
                "score": stab_raw(s),        # 近 N 次真实调用成功率（展示用；没数据 → None）
                "stab": stab_of(s),          # 带小样本收缩的稳定分（排序用；没数据 → None）
                "samples": len(s.get("win") or []),
            })
    models = []
    for m, chans in agg.items():
        ms = model_status.get(m)
        any_ok = any(c["available"] for c in chans)
        any_cool = any(c["in_cooldown"] or c["preempted"] for c in chans)
        all_down = bool(chans) and all(c["down"] for c in chans)
        tested = ms is not None
        reason = (ms.get("reason", "") if ms else "")
        ms_state = ms.get("state") if ms else None
        if all_down or (ms is not None and (ms_state == "down"
                        or (ms_state is None and not ms.get("available")))):
            status = "down"          # 全部渠道硬失败（402/403/下线 等）
            available = False
        elif any_ok:
            status = "ok"            # 免费且当前可调
            available = True
        elif any_cool or ms_state == "limited":
            status = "limited"       # 暂时限流/冷却/预判跳过中，仍算可用范畴
            available = False
        else:
            status = "down"
            available = False
        _tier = capability.tier_of(m, cfg.get("model_tiers"))
        models.append({
            "id": m,
            "available": available,
            "status": status,
            "tested": tested,
            "test_reason": reason,
            "test_ts": (ms.get("ts", 0) if ms else 0),
            "any_channel_available": any_ok,
            "channel_count": len(chans),
            "tier": _tier,
            "aa": capability.bench_of(m),              # Artificial Analysis 智能指数（没上榜 → None）
            "cap_score": capability.capability_score(m, _tier, cfg.get("model_tiers")),   # 连续能力分（排序用）
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
                if (ch.get("enabled", True) and cs and cs.valid and t in cs.models
                        and not channel_cooling(ch["id"], now)
                        and (t, ch["id"]) not in channel_down
                        and (t, ch["id"]) not in unverified
                        and cooldown.get((t, ch["id"]), 0) <= now):
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
    # 命名与「模型」页视图一一对应：每个视图都有 auto:<名> 写法（不再保留裸 `auto` 简写，
    # 用户拍板：`auto:balanced` 已经对应均衡，再留一个 `auto` 就是重复）。
    "auto:balanced": "balanced",
    "auto:quality": "quality",
    "auto:stability": "stability",
    "auto:speed": "speed",
    # 视觉分组：只挑「能看图」的模型，再按能力优先 + 稳定/速度均衡排序。
    # 用途：Hermes 的「辅助视觉模型」直接填 auto:vision（失败自动换下一个能看图的模型）。
    "auto:vision": "vision",
}


def is_reserved_auto(model: str) -> bool:
    return model in RESERVED_AUTO


def auto_strategy_of(model: str) -> str:
    return RESERVED_AUTO.get(model, "balanced")


def candidates_for_auto(strategy: str, cfg: dict) -> list:
    """给「auto」请求生成候选：**所有当前可用 (模型, 渠道) 组合**，按策略排序。

    注意这里就是 auto 的「切换顺序」——`main.chat_completions` 会从这个列表**逐个尝试**
    直到成功，所以「能用的排前面」在本函数里已经保证：冷却中 / 渠道熔断 / 429 预判 /
    模型级 down 的 (模型,渠道) 全部被过滤掉，列表里不会出现用不了的组合。

    排序规则与界面一致：收藏的模型优先（组内仍按策略综合分），然后才是未收藏模型。
    strategy="vision" 时额外只保留「能看图」的模型（Hermes 辅助视觉模型用）。"""
    now = time.time()
    pinned = set(cfg.get("pinned") or [])
    need_vision = strategy == "vision"
    out, seen = [], set()
    preempt = cfg.get("adaptive_preemption", True)
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        cs = channels.get(ch["id"])
        if not cs or not cs.valid:
            continue
        if channel_cooling(ch["id"], now):
            continue  # 渠道级熔断（账号级限流）中
        for m in cs.models:
            key = (m, ch["id"])
            if key in seen or key in channel_down:
                continue
            if need_vision and not capability.meta_of(m)["vision"]:
                continue  # 视觉分组：只留能看图的
            if ratelimit_exhausted(m, ch["id"], now):
                continue
            if cooldown.get(key, 0) > now:
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
    """某渠道「当前可用」的模型数：不在冷却、未被模型级标记不可用、渠道未熔断"""
    cs = channels.get(cid)
    if not cs:
        return 0
    if channel_cooling(cid):
        return 0
    now = time.time()
    avail = 0
    for m in cs.models:
        key = (m, cid)
        if cooldown.get(key, 0) > now or key in unverified or key in channel_down:
            continue
        ms = model_status.get(m)
        if ms is not None and not ms.get("available"):
            continue
        avail += 1
    return avail
