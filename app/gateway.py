"""运行时状态：渠道健康、模型注册表、评分排序、错误分类冷却"""
import collections
import hashlib
import logging
import math
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
# 模型级测试记录。**自 2026-09-21（用户拍板 A1）起不再参与任何可用性 / 路由判定**，只用于
# 界面展示「最近一次测过什么」（tested / test_reason / test_channel / test_ts）。
# 能不能用一律由 (模型,渠道) 级状态决定（channel_down / cooldown / unverified / ratelimit）——
# 只有带渠道维度的记录才不会被跨渠道污染。详见 mark_model_status 与 model_view 的说明。
model_status: dict = {}  # model_id -> {"available": bool, "state": str, "reason": str, "ts": float, "channel": str}
channel_down: dict = {}  # (上游模型ID, channel_id) -> {"reason": str, "ts": float} 该渠道上此模型硬不可用
# 上游响应头里的官方限流信息（Groq/NIM 的 x-ratelimit-*；魔搭的 modelscope-ratelimit-*）：
# (模型, 渠道) -> {"remaining": int, "reset_ts": float, "source": str, "limit": int|None}
ratelimit: dict = {}
# 魔搭 ModelScope 的**账号级**日额度（`modelscope-ratelimit-requests-remaining`）：
# 该 Key 下所有模型共享，粒度是「渠道」而不是「模型×渠道」，塞不进 ratelimit，所以单开一张表。
# channel_id -> {"remaining": int, "limit": int|None, "reset_ts": float}
user_quota: dict = {}
# 各口径限流头的命中计数（排障用）：重启后看一眼就知道魔搭的头到底有没有被读到
ratelimit_hits: dict = {"modelscope": 0, "x-ratelimit": 0}
last_probe_ok: dict = {}  # (模型, 渠道) -> 上次探测成功的 ts（魔搭等按次计费平台用于省探测预算）

# ---- 渠道级 429 熔断（修魔搭等账号级限流：整个 Key 被限，所有模型一起 429）----
_CH429_WINDOW = 600    # 10 分钟窗口
_CH429_MIN_MODELS = 2  # 窗口内 ≥2 个不同模型撞 429 → 判定账号级限流
_CH429_COOL = 600      # 渠道冷却 10 分钟
_channel_429_events: dict = {}  # channel_id -> deque[(ts, model_id)]
channel_cool: dict = {}  # channel_id -> 冷却截止时间
channel_last_ok: dict = {}  # channel_id -> 最近一次成功请求的 ts（区分账号级/按模型限流）

# ---- 额度型 429（free_daily）：模型级 vs 账号级 ----
# 上面那套是「10 分钟窗口内 ≥2 个模型」的分钟级熔断；这里判的是额度型 429 的**层级**：
# 免费额度型渠道普遍是两层限额（账号级 + 模型级），429 正文分不出来 → 按**爆发度**判。
# 为什么用「短窗口内多少个不同模型」而不是「当天累计多少」：账号级额度耗尽是**一瞬间**的
# （额度见底之后每一个模型都立刻 429），模型级则是**一天里慢慢攒**（大模型日额度只有 100）。
# 用当天累计会把「一天内先后耗完的 4 个大模型」误判成账号级 → 反而又误伤一整条渠道。
_CH429_QUOTA_WINDOW = 1800   # 30 分钟
_CH429_QUOTA_MODELS = 4      # 窗口内 ≥4 个不同模型撞额度 429 → 判账号级（见 note_channel_quota_429）
# channel_id -> deque[(ts, 上游模型ID)]
# 不落盘：重启后从 0 重新累计，代价只是再撞几次 429；而账号级判定后的 channel_cool 是落盘的。
channel_quota_429: dict = {}

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
    for cid_ in list(user_quota):
        if cid_ not in ids:
            user_quota.pop(cid_, None)
    for key in list(last_probe_ok):
        if key[1] not in ids:
            last_probe_ok.pop(key, None)
    for cid in list(channel_cool):
        if cid not in ids:
            channel_cool.pop(cid, None)
            _channel_429_events.pop(cid, None)
    for cid in list(channel_quota_429):
        if cid not in ids:
            channel_quota_429.pop(cid, None)
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
    free_daily   → 冷却到明天凌晨（免费额度按天刷新，次日自动回来）。
                   **这只是「该模型冷却到明天」**；要不要升级成整渠道停摆，由调用方用
                   `note_channel_quota_429` 按「短窗口内几个不同模型撞过」判定（见那里）
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


# 启动归正（restore_model_status）专用的「永久」判据 —— **刻意比 `_PERMANENT_KEYWORDS` 窄**。
# 为什么不能共用：上面那套是判**上游响应正文**的，含「已用完 / 用尽」这类**额度进展**措辞；
# 而归正函数判的是**我们自己写的 reason**，其中每日额度冷却的文案里就有「额度已用完 / 用尽」
# → 会把「明天就恢复」的 limited 在下次重启时静默升成永久 down（down 没有出口 → 再也回不来）。
# 2026-09-21 核查：当时 limited 记录里只有 1 条命中（`余额不足，请充值`，本就是 down 语义），
# 所以这条是**潜在**坑；但同日新增的额度 429 文案（「该模型额度已用完，冷却到明天」）正好
# 会踩中它，不修就等于把白天的修复在重启时自己撤销掉。
_DOWN_REASON_MARKERS = (
    "http 402", "http 403", "http 404", "http 405",   # reason 里的状态码串
    "余额", "充值", "欠费", "资源包", "购买", "非免费", "所有提供方均收费",
    "insufficient", "balance", "recharge", "top up", "purchase",
    "never purchase", "only available", "credit balance", "no credit",
)
# 注意：这里**故意不收裸 "credit"**。OpenRouter 免费模型的日额度 429 正文是
# 「Rate limit exceeded: free-models-per-day. **Add 10 credits** to unlock 1000 free
# model requests per day」——裸 "credit" 会命中它，把一条「明天就恢复」记成永久 down。
# 这句英文的真实含义是「你现在每天只有 50 次，想升到 1000 次请充 10 美元」，**不是余额不足**。


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


# 本机网络类失败的文本特征（**只用于「整轮全挂」判定**，见 is_local_net_error）
_LOCAL_NET_HINTS = (
    "getaddrinfo", "11001", "11004",               # Windows DNS 解析失败（唤醒后典型）
    "name resolution", "name or service not known", "nodename nor servname",
    "temporary failure in name resolution",
    "network is unreachable", "10051", "10065", "10060",   # 网络不可达/主机不可达/连接超时
    "all connection attempts failed", "connecterror", "connect: ",
)


def is_local_net_error(text) -> bool:
    """这条失败是不是「本机网络没就绪」（DNS 解析不了 / 根本连不出去）。

    **只在「整轮所有渠道一起挂」时才采信**（`main._note_sweep_health`）：单独一个渠道
    报 connect 类错误不能当本机问题 —— 那可能是那家上游自己的事。整轮全挂才是本机的形状，
    典型场景就是**睡眠/休眠唤醒后 DNS 还没起来**（2026-09-16 实测：七个渠道同时
    `[Errno 11001] getaddrinfo failed`，而 15 分钟后网络早已恢复）。

    注意：这里**不改** `kind_from_error` 的口径 —— 那个函数决定稳定分要不要记账，
    放宽会把「上游老是断连」也放过（`_STAB_SKIP_KINDS` 含 `local_net`）。
    """
    t = str(text or "").lower()
    return any(k in t for k in _LOCAL_NET_HINTS)


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
        # 第 6 位是 cancelled（老库/老调用可能没有这一列）。注意：**成功的中断行要留下**
        # （模型已正常输出、客户端先走，口径见 `_stream_ok`），只有「没成功的中断」才跳过。
        if len(row) > 5 and row[5] and not success:
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


# ---- 上游官方限流响应头：平台自己报的剩余额度，比自学水位精确 ----
_MS_DAILY_TZ_OFFSET = 8 * 3600   # 魔搭日额度按 UTC+8 00:00 刷新
_RL_NO_RESET_FALLBACK = 300.0    # 通用口径没给重置时刻时的兜底窗（见下）


def _parse_duration(s: str) -> float:
    """解析 Groq 风格时长 "2m39.5s" / "1h" / "45s"，失败返回 0"""
    import re
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$", (s or "").strip())
    if not m or not any(m.groups()):
        return 0.0
    h, mi, sec = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


def _header_int(headers, name: str):
    """读一个数值型响应头；缺失或非数字（如 "unlimited"）一律返回 None，不抛异常。

    取值失败就当作「这头不存在」，而不是让整个函数挂掉 —— 上游头名/格式随时会变，
    解析必须能安全降级。"""
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _next_ms_daily_reset(now: float = None) -> float:
    """下一个 UTC+8 00:00 的 epoch 秒（+60s 缓冲，避开刷新瞬间的竞态）。

    魔搭 API-Inference 的日额度「每日 UTC+8 00:00 重置，不跨天累计」，但
    **响应头只给剩余数、不给重置时刻**，所以必须自己算出来。

    为什么非要有值：`ratelimit_exhausted` 只在 `reset_ts` 是正数且已过期时才解除跳过，
    `reset_ts=0` 会让那一格**永久拉黑**。魔搭这条路径不允许出现 0。"""
    now = now if now is not None else time.time()
    g = time.gmtime(now + _MS_DAILY_TZ_OFFSET)   # 先平移成 UTC+8 日历，再数「今天过了多少秒」
    into_day = g.tm_hour * 3600 + g.tm_min * 60 + g.tm_sec
    return now + (86400 - into_day) + 60


def note_ratelimit_headers(cid: str, mid: str, headers) -> bool:
    """从响应头读平台给的官方额度信息并记录。返回 True = 本次读到有效数据。

    两套口径并存，**先魔搭后通用**（头名不冲突，各自独立判据）：

    **① 魔搭 ModelScope**（`modelscope-ratelimit-*`；官方 API-Inference limits 文档有据，
    **成功响应也带**，不像 OpenRouter 只有 429 才带）
      - `...-model-requests-remaining` = 该**模型**当天剩余 → 粒度正好是 (模型,渠道)，
        写进 `ratelimit`；归零后 `ratelimit_exhausted` 会让路由跳过这一格，不用撞墙。
      - `...-requests-remaining`       = 该**账号**当天剩余（跨所有模型共享）→ 写 `user_quota`；
        归零后该 Key 下所有模型都会 429，直接走既有的 `mark_channel_quota_exhausted`。
      - 两个 `...-limit` 一并记下：官方会**动态调整**单模型上限（大模型只有 100/天，
        新模型/濒临下线还会再降），所以「上限」要从头里读，不能写死 500。

    **② 通用 `x-ratelimit-*`**（Groq / NIM；OpenRouter 只在 429 时带）
      - `x-ratelimit-remaining-requests` / `x-ratelimit-remaining` 配
        `x-ratelimit-reset-requests` / `x-ratelimit-reset` 的**时长**（如 "2m39.5s"）。
      - 没带 reset 时刻时兜底 `_RL_NO_RESET_FALLBACK`，**不再写 0**：旧行为下
        `remaining=0` + 没 reset 头 = 该模型被永久跳过，只有重启才解得开。"""
    hit = False

    # ---- ① 魔搭：模型级 + 账号级 ----
    ms_model = _header_int(headers, "modelscope-ratelimit-model-requests-remaining")
    if ms_model is not None:
        cur = ratelimit.setdefault((mid, cid), {})
        cur["remaining"] = ms_model
        cur["reset_ts"] = _next_ms_daily_reset()
        cur["source"] = "modelscope"
        limit = _header_int(headers, "modelscope-ratelimit-model-requests-limit")
        if limit is not None:
            cur["limit"] = limit
        hit = True

    ms_user = _header_int(headers, "modelscope-ratelimit-requests-remaining")
    if ms_user is not None:
        user_quota[cid] = {
            "remaining": ms_user,
            "limit": _header_int(headers, "modelscope-ratelimit-requests-limit"),
            "reset_ts": _next_ms_daily_reset(),
        }
        hit = True
        # 账号级见底：整条渠道当天都调不动，立刻停到明天（不等第二个模型撞 429）
        if ms_user <= 0 and not channel_cooling(cid):
            mark_channel_quota_exhausted(cid, "魔搭账号级日额度用完")

    if hit:
        n = ratelimit_hits.get("modelscope", 0) + 1
        ratelimit_hits["modelscope"] = n
        if n == 1:
            # 本进程首次读到魔搭额度头 → 明确留痕。这条日志是「流式响应到底带不带这 4 个头」
            # 的判据：`grep 魔搭官方额度 data/api-hub.log`，有 = 接上了；一直没有 = 头没送上来，
            # 别再怀疑解析代码。
            logging.getLogger("api-hub").info(
                "读到魔搭官方额度头：模型[%s] 剩余 %s / 账号剩余 %s（%s）",
                mid, ms_model, ms_user, cid)
        return True

    # ---- ② 通用 x-ratelimit-* ----
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
            "reset_ts": time.time() + (secs or _RL_NO_RESET_FALLBACK),
            "source": "x-ratelimit",
        }
        ratelimit_hits["x-ratelimit"] = ratelimit_hits.get("x-ratelimit", 0) + 1
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


def note_channel_quota_429(cid: str, mid: str, now: float = None) -> tuple:
    """额度型 429（`classify_429` 判 `free_daily`）到底是**模型级**还是**账号级**？

    返回 `(是否账号级, 最近窗口内撞过的不同模型数)`。

    背景（2026-09-20 取证）：魔搭是**两层**限额——账号级 2000 次/天（该 Key 下全模型共享）
    + 模型级 500 次/天（大模型仅 100 次/天）。两者撞限时的 429 正文**一模一样**
    （`{"error":{"message":"insufficient balance"}}`），从响应里分不出来。
    旧做法是「一见额度 429 就整条渠道停到明天」→ 一个大模型（日额度只有 100）先耗完，
    撞一次就把整条渠道封十几个小时。

    判据用**爆发度**：窗口内（30 分钟）有几个不同模型撞额度 429。
    近三周 62 次魔搭 429 实测——
      - **小簇**（窗口内 1~3 个模型）：渠道**仍在正常服务**（有成功请求为证）→ 判模型级，
        只冷这一个 (模型,渠道)，渠道其余模型照常路由；
      - **大簇**（19 / 15 / 8 个模型在同一两分钟内一起撞）：整条渠道确实中断 → 判账号级，
        调 `mark_channel_quota_exhausted` 停到明天。
    阈值 4 正好把这两类分开。代价上界：真账号级时最多白撞 4 次 429（这 4 个模型本来也确实
    已经不可用），换来的是一条渠道不会被单个模型拖停一整天。

    同样的误伤也发生在 OpenRouter `:free` 上（那是**按模型** 50 次/天）——一并修好。
    """
    now = now if now is not None else time.time()
    dq = channel_quota_429.setdefault(cid, collections.deque(maxlen=200))
    dq.append((now, mid))
    while dq and dq[0][0] < now - _CH429_QUOTA_WINDOW:
        dq.popleft()
    n = len({m for _, m in dq})
    if n >= _CH429_QUOTA_MODELS:
        mark_channel_quota_exhausted(cid, f"{_CH429_QUOTA_WINDOW // 60} 分钟内 {n} 个不同模型撞额度 429 → 账号级")
        return (True, n)
    return (False, n)


def quota_429_models(cid: str, now: float = None) -> int:
    """最近窗口内撞过「额度型 429」的**不同模型数**（排障用：看它离判定阈值还有多远）"""
    dq = channel_quota_429.get(cid)
    if not dq:
        return 0
    now = now if now is not None else time.time()
    return len({m for ts, m in dq if ts >= now - _CH429_QUOTA_WINDOW})


def mark_model_status(model: str, available: bool, reason: str = "", channel: str = "",
                      state: str = None):
    """记一次模型级测试/调用的真实结果并持久化。

    state: 'ok' 免费可调 / 'limited' 暂时受限（429/5xx/连接，冷却后会恢复）/ 'down' 硬不可用。
    available 由 state 唯一决定：只有 'ok' 才可路由。'limited'（受限）也**不可路由**——
    否则「余额不足/限流」的模型会冒充可用，一用就报错。

    **2026-09-21（用户拍板 A1）起，这里写下的状态不再影响能不能路由**：路由与界面可用性一律
    看 (模型,渠道) 级记录，「这条渠道不可用」由 `mark_channel_down` / `mark_result`(冷却) 承担。
    本函数的产出退化为展示信息（最近一次测过什么、在哪条渠道测的）。`available` 字段保留只为
    兼容磁盘旧数据与前端读取，已不再被任何判定逻辑消费。之所以要退这一步：模型级状态没有渠道
    维度，一次渠道硬失败（或渠道停用后遗留的记录）会把该模型**所有**渠道一起判红。"""
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
    # 归正判据用**更窄**的一套（见 _DOWN_REASON_MARKERS）：这里比的是我们自己写的 reason，
    # 不是上游正文，共用 _PERMANENT_KEYWORDS 会把「额度已用完/用尽」这种**每日额度**文案
    # 误判成永久失败。
    _PERM_REASONS = _DOWN_REASON_MARKERS
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
            # 魔搭账号级日额度：只存还没到刷新点的，过期的重启后自然重学
            "user_quota": {cid: v for cid, v in user_quota.items()
                           if (v.get("reset_ts") or 0) > now},
            # 限流头命中计数：落盘是为了「重启后还没发请求时」也能看出魔搭的头有没有被读到
            "ratelimit_hits": dict(ratelimit_hits),
            "channel_cool": {cid: round(v, 1) for cid, v in channel_cool.items() if v > now},
            # 会话粘性（auto 用）：重启后同一个会话继续用同一个模型，不因为重启而换模型
            "sticky": {f"{s}|{k}": v for (s, k), v in sticky.items()
                       if (v.get("ts") or 0) + STICKY_TTL > now},
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
    # 魔搭账号级日额度：只收还没到刷新点的（到点了重启后重新从响应头学）
    for cid, v in (data.get("user_quota") or {}).items():
        if isinstance(v, dict) and "remaining" in v and (v.get("reset_ts") or 0) > now:
            user_quota[cid] = v
    for src, n in (data.get("ratelimit_hits") or {}).items():
        if isinstance(n, int):
            ratelimit_hits[src] = n
    # 恢复渠道级冷却（含「账户级当天额度用完」的冷却到明天）
    for cid, v in (data.get("channel_cool") or {}).items():
        if v > now:
            channel_cool[cid] = v
    # 恢复会话粘性（auto 用）：只收还没过闲置期的
    n_sticky = 0
    for k, v in (data.get("sticky") or {}).items():
        s, _, key = k.partition("|")
        if not (s and key and isinstance(v, dict)):
            continue
        ts = v.get("ts") or 0
        if ts + STICKY_TTL <= now or not v.get("model"):
            continue
        sticky[(s, key)] = {"model": v.get("model"), "channel": v.get("channel") or "",
                            "ts": ts, "label": v.get("label") or ""}
        n_sticky += 1
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
        "已恢复运行时状态：冷却 %d，待验证 %d，渠道级硬失败 %d，限流头 %d，429 预判 %d，会话粘性 %d"
        "（seed 冷却 %d，评分样本 %d）",
        n_cool, len(unverified), len(channel_down), len(ratelimit),
        len(throttle.learned_pairs()), n_sticky, n_seeded, n_stat)


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
# 速度维：延迟 → 0~1，用 **log 线性化**（100ms→1.0，60s→0.0）。
# 旧版是双曲映射 400/(400+ms)：看着合理，但它极度右偏，真实样本全挤在 (0, 0.1)，
# 而 `_score_dims` 是「主维度按 band 分档」→ 等价于「延迟 <3.6s 全算同一档」，
# 速度优先视图里 13 个收藏有 12 个落在桶 0、**桶内速度完全不起作用**（用户 2026-09-20 报）。
# log 线性化后各档之间是等比延迟（band 0.10 ≈ 1.9 倍），快慢两端粒度一致。
# ⚠️ 改这里必须同步改前端 `speedScore`（frontend/index.html 的 compScore），
#    否则「界面显示的顺序」和「auto 实际选路」会分叉——那比现在更糟。
_SPEED_FAST_MS = 100.0     # ≤100ms → 满分 1.0
_SPEED_SLOW_MS = 60000.0   # ≥60s → 0.0
_SPEED_LOG_SPAN = math.log(_SPEED_SLOW_MS / _SPEED_FAST_MS)


def speed_score(lat_ms) -> float:
    """延迟(ms) → 0~1 速度分（log 线性化，见上）。None / 非法值 → 0（未知按最慢算）。"""
    if lat_ms is None:
        return 0.0
    try:
        x = float(lat_ms)
    except (TypeError, ValueError):
        return 0.0
    if x <= _SPEED_FAST_MS:
        return 1.0
    if x >= _SPEED_SLOW_MS:
        return 0.0
    return (math.log(_SPEED_SLOW_MS) - math.log(x)) / _SPEED_LOG_SPAN


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
        speed = speed_score(lat)
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
    ov = capability.tier_overrides(cfg)     # 手动档位：精确表 > 正则表
    tier = capability.tier_of(c["model"], ov)
    cap = capability.capability_score(c["model"], tier, ov)
    speed = speed_score(lat)
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

    **2026-09-21 起也不再排除 `channel_down`（用户拍板）**：`down` 没有过期时间，只靠
    「一次成功调用」清除，而能发起那次调用的入口本来全被它自己挡住（自动路由排除、
    本函数也排除）→ 误判成 down 的模型再也回不来，只能去渠道卡片「扫描全部模型」，
    粒度还连带整条渠道。让手动测试穿透 `channel_down`：**错的能救回、真死的点几次
    还是同一句错**（比"一律放回 limited"少一层假乐观——真用不了的模型不该回到候选里）。
    代价：在真·硬失败（余额不足 / 已下线）的模型上点测试会真发一次上游请求。

    仍排除：渠道未启用 / 健康检查未通过 / 模型不在该渠道 /
    官方限流头显示额度耗尽未重置(ratelimit_exhausted)——它和 down 不同，是**自愈**的
    时间窗封锁（到点自动解），点了也还是失败，没必要白烧一次请求。
    返回结构与 candidates_for 一致；排序是**两段式**：未记硬失败的渠道在前，组内按策略
    综合分（2026-09-21 调整，见函数末尾注释——此前是纯综合分，会把死渠道排到活渠道前面，
    点一次测试先在死渠道上白等两分钟）。"""
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
            if key in seen:
                continue
            if ratelimit_exhausted(mid, ch["id"], now):
                continue
            seen.add(key)
            out.append({"channel": ch, "model": mid})
    strategy = cfg.get("route_strategy", "balanced")
    # 2026-09-21（用户拍板）：先分两组 —— **没被记过硬失败的渠道排在前面**，组内再按策略分。
    # 手动测试要回答的是「这个模型现在能不能用」，所以先打有希望的渠道；已知硬失败的渠道
    # 仍留在候选末尾（Plan B 的出口不丢：活的都失败后才轮到它，用来确认它是否已恢复）。
    # 动因（线上实测）：`z-ai/glm-5.3-flash` 在 OpenRouter 有 402 记录、NIM 无失败记录，
    # 但 OR 的稳定维按先验给分、NIM 的 38.8s 延迟被速度维拖死 → OR 反而排前面，点一次测试
    # 白等 122s 才拿到那句 402，而模型其实在 NIM 上可用。分组排序后这类倒挂不会再发生。
    out.sort(key=lambda c: (0 if (c["model"], c["channel"]["id"]) not in channel_down else 1,
                            -_composite(c, cfg, strategy)))
    return out


def model_view(cfg: dict) -> list:
    """模型视图：状态三档 —— ok(免费可调/绿) / limited(暂时限流或冷却/黄) / down(硬不可用/红)

    语义：暂时超过额度被限流的模型仍算「可用」范畴，只是标注受限；仅 402/403/下线 等
    硬失败才记为 down，且按 (模型, 渠道) 粒度记录——只有所有渠道都硬失败才算整个模型 down。
    冷却到期但未实测确认（unverified）、429 预判跳过（preempted）、渠道级熔断（账号级限流）
    都会让该渠道显示受限，避免「界面绿色但实际用不了」。

    **2026-09-21（用户拍板 A1）**：`model_status`（模型级）**不再参与这里的任何判定**，
    模型能不能用完全由渠道级状态聚合得出。它只提供 `tested / test_reason / test_channel / test_ts`
    这几个展示字段。上面那句「只有所有渠道都硬失败才算整个模型 down」现在才是真的成立 ——
    在此之前，任何一次渠道硬失败写下的模型级 down 都会推翻它，把健康渠道一起判红。"""
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
            s = get_stat(m, ch["id"])
            # 「这条渠道上这个模型为什么不可用」——只给界面 chip 悬停解释用。
            # 判定次序就是下面 available 表达式的取反，保证两处永远一致（改了这里必须同步改那里）。
            if cs.valid is not True:
                unavail = "渠道健康检查未通过"
            elif ch_down:
                _r = (channel_down.get(key) or {}).get("reason") or "已下线/无权限/需充值"
                unavail = "硬失败：" + _r
            elif ch_cool:
                unavail = "渠道级冷却（账号级限流），到点自动恢复"
            elif key in unverified:
                unavail = "冷却已到期，待实测确认"
            elif in_cool:
                unavail = "限流冷却至 " + time.strftime(
                    "%H:%M", time.localtime(cooldown.get(key, now)))
            elif preempted:
                unavail = "限流预判，本轮跳过（等额度刷新）"
            else:
                unavail = ""
            entry.append({
                "channel_id": ch["id"],
                "channel_name": ch.get("name") or ch["type"],
                # 渠道类型：前端搜索要按「平台」筛（provider:modelscope / provider:nim），
                # 只靠展示名匹配的话，用户把渠道改名成「小魔搭」就搜不到了。
                "channel_type": ch["type"],
                # 2026-09-21（用户拍板 A1）：**去掉模型级否决**。原式末尾还有 `and not model_down`，
                # 它让某条渠道的一次硬失败（或渠道停用后遗留的记录）把该模型的**所有**渠道一起判红。
                # 实测受害者：z-ai/glm-5.3 的 NVIDIA NIM（从没失败过）、stepfun-ai/Step-3.x-Flash
                # 唯一还启用的魔搭（失败记录来自已停用的 HuggingFace）。现在只看这条渠道自己的状态。
                "available": bool(cs.valid) and not ch_cool and not ch_down
                             and not in_cool and not preempted,
                "in_cooldown": in_cool or ch_cool,
                "down": ch_down,
                "preempted": preempted,
                "unavail_reason": unavail,
                # 响应速度：首字节(TTFT) 优先 → 实测总延迟 → 渠道健康检查延迟
                # ——与 _composite / _model_composite / FE compScore 同一口径
                "latency_ms": s.get("ttft") or s["latency"] or cs.latency_ms,
                "score": stab_raw(s),        # 近 N 次真实调用成功率（展示用；没数据 → None）
                "stab": stab_of(s),          # 带小样本收缩的稳定分（排序用；没数据 → None）
                "samples": len(s.get("win") or []),
            })
    models = []
    ov = capability.tier_overrides(cfg)     # 手动档位表：算一次，别在 600+ 个模型的循环里反复合成
    for m, chans in agg.items():
        ms = model_status.get(m)
        any_ok = any(c["available"] for c in chans)
        any_cool = any(c["in_cooldown"] or c["preempted"] for c in chans)
        all_down = bool(chans) and all(c["down"] for c in chans)
        tested = ms is not None
        reason = (ms.get("reason", "") if ms else "")
        # 2026-09-21（用户拍板 A1）：模型级状态**退出可用性判定**，只保留「最近一次测过什么」的
        # 展示职责（test_reason / test_channel 给界面）。以前 model_status[模型].available=False
        # 会一票否决该模型的**所有**渠道；而 mark_model_status 的 key 不带渠道维度，于是渠道被
        # 停用/删除后留下的模型级 down 仍会继续压住其他还在启用的渠道。现在「能不能用」纯由
        # 渠道级状态聚合 —— 与上面 docstring 里「只有所有渠道都硬失败才算整个模型 down」一致。
        if any_ok:
            status = "ok"            # 至少一条渠道可调
            available = True
        elif all_down:
            status = "down"          # 全部渠道硬失败（402/403/下线 等）
            available = False
        elif any_cool:
            status = "limited"       # 暂时限流/冷却/预判跳过中，等得到
            available = False
        else:
            status = "down"          # 兜底：渠道健康检查未通过等
            available = False
        # 手动档位：界面点 chip 写的（精确表）优先于 config 里手改的正则表
        _manual = capability.override_tier(m, ov) is not None
        _tier = capability.tier_of(m, ov)
        models.append({
            "id": m,
            "available": available,
            "status": status,
            "tested": tested,
            "test_reason": reason,
            # 最近一次测试是在哪条渠道上做的（模型级记录自带 channel 名）。多渠道模型必须显示它，
            # 否则用户看到「最近实测：HTTP 403」却不知道是哪条渠道的账。
            "test_channel": ((ms.get("channel") or "") if ms else ""),
            "test_ts": (ms.get("ts", 0) if ms else 0),
            "any_channel_available": any_ok,
            "channel_count": len(chans),
            "tier": _tier,
            "tier_manual": _manual,                    # 被手动指定过（界面画标记 + 弹层里对比「自动判定」）
            # 纯自动判定（不带手动覆盖）：只在手动过时才算，给弹层显示「当前判定：X」用
            "tier_auto": capability.tier_of(m) if _manual else _tier,
            "aa": capability.bench_of(m),              # Artificial Analysis 智能指数（没上榜 → None）
            "cap_score": capability.capability_score(m, _tier, ov),   # 连续能力分（排序用）
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
    # 命名与「模型」页视图一一对应：每个视图都有 auto-<名> 写法。2026-09-21 用户拍板：
    # 冒号版（auto:balanced 等）不规范且部分客户端模型名正则限死 [A-Za-z0-9._/-]，
    # 冒号别名全部删除，只保留连字符写法（例：vibe-astock 复盘 Agent 的 codex 引擎）。
    "auto-balanced": "balanced",
    "auto-quality": "quality",
    "auto-stability": "stability",
    "auto-speed": "speed",
    # 视觉分组：只挑「能看图」的模型，再按能力优先 + 稳定/速度均衡排序。
    # 用途：Hermes 的「辅助视觉模型」直接填 auto-vision（失败自动换下一个能看图的模型）。
    "auto-vision": "vision",
}


def is_reserved_auto(model: str) -> bool:
    return model in RESERVED_AUTO


def auto_strategy_of(model: str) -> str:
    return RESERVED_AUTO.get(model, "balanced")


# ---- 会话粘性（**只对 auto-* 生效**）----
# 起因（2026-09-20 用户报）：「模型正常输出，在同一个对话中也会出现模型切换」。
# 根因不在成败判据，而在候选排序的第一顺位是**收藏**：auto 每个请求都重新算一遍候选，
# 收藏的模型一过冷却就抢回第一、下次失败又让位 → 一个会话里来回换（取证见
# PROGRESS.md「auto 路由 · 为什么正常输出也会换模型」）。
# 粘性让「本会话上一次**成功产出**的模型」保持第一，只有它失败/不可用才换。
# 优先级：**粘性 > 收藏 > 策略分**。
# ⚠️ 只认「成功产出」：上游返回 200 但流中断、或一个字没吐就断，都不算（不写粘性）。
STICKY_TTL = 7200.0   # 闲置多久算这次会话结束（每成功一次滑动续期）
STICKY_MAX = 300      # 最多记多少个 (策略, 会话)，超出按最久没用的丢

sticky: dict = {}     # (策略, 会话键) -> {"model", "channel", "ts", "label"}

# 客户端愿意发这些头就用它当会话 id（最准）；不发则退回「首条 user 消息」指纹
STICKY_HEADERS = ("x-session-id", "x-conversation-id", "x-chat-id", "conversation-id")


def _msg_text(m: dict) -> str:
    """取一条消息的文本内容（content 可能是字符串，也可能是多模态片段数组）"""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif p.get("type"):
                parts.append(f"<{p['type']}>")   # 图片/音频只记类型，不记内容
        return "".join(parts)
    return ""


def session_key_of(body: dict, header_id: str = "") -> tuple:
    """认出「同一个会话」→ `(键, 来源, 可读标签)`；认不出返回 `("", "", "")`（= 不粘）。

    OpenAI 协议里**没有会话字段**（Hermes 这类客户端也不一定发），所以按这个顺序认：
      1. 显式请求头（`X-Session-Id` / `X-Conversation-Id` …）——客户端愿意发就用它，最准；
      2. **第一条 user 消息的指纹**——客户端每轮都把完整历史发上来（实测同一会话
         prompt_tokens 连续增长 66k→73k），所以首条 user 消息在整个会话里不变。
    认不出的情况（只发最后一轮、messages 为空、首条消息没文本）**一律不粘**，
    退回原策略排序——这是安全降级：粘性失效 = 老行为，不会更糟。
    ⚠️ 若某个客户端的首条 user 消息里塞了每轮都变的动态内容（时间戳/环境块），
    指纹就会每轮都变、粘性永远不命中；`label` 会原样进日志，一眼能看出来。"""
    h = (header_id or "").strip()
    if h:
        return hashlib.sha1(("hdr:" + h).encode("utf-8", "ignore")).hexdigest()[:12], "header", h[:40]
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "", "", ""
    first_user = next((m for m in msgs
                       if isinstance(m, dict) and m.get("role") == "user"), None)
    m0 = first_user if first_user is not None else msgs[0]
    if not isinstance(m0, dict):
        return "", "", ""
    txt = _msg_text(m0)
    if not txt.strip():
        return "", "", ""
    src = "first_user" if first_user is not None else "first_msg"
    label = " ".join(txt.split())[:40]
    # 来源也进哈希：避免「首条消息」与「首条 user 消息」文本相同时撞键
    return hashlib.sha1(f"{src}|{txt}".encode("utf-8", "ignore")).hexdigest()[:12], src, label


def _sticky_trim(now: float):
    """淘汰过期条目；仍超上限就丢最久没用的"""
    for k, v in list(sticky.items()):
        if (v.get("ts") or 0) + STICKY_TTL <= now:
            sticky.pop(k, None)
    if len(sticky) > STICKY_MAX:
        for k, _ in sorted(sticky.items(), key=lambda kv: kv[1].get("ts") or 0
                           )[:len(sticky) - STICKY_MAX]:
            sticky.pop(k, None)


def mark_sticky(strategy: str, key: str, model: str, cid: str, label: str = "") -> bool:
    """记下「这个会话这一次是谁成功服务的」。返回是否发生了**换模型**（用于日志）。"""
    if not (strategy and key and model):
        return False
    now = time.time()
    prev = sticky.get((strategy, key))
    sticky[(strategy, key)] = {"model": model, "channel": cid, "ts": now,
                               "label": label or (prev or {}).get("label") or ""}
    _sticky_trim(now)
    _request_save()   # 节流 1s 合并写盘；重启后粘性还在（见 runtime_state.json）
    return bool(prev) and prev.get("model") != model


def clear_sticky(strategy: str = "", key: str = "", model: str = "") -> int:
    """清粘性（不传参数 = 全清）：换模型的兜底手段 + 排障用。"""
    n = 0
    for k in list(sticky):
        s, kk = k
        if strategy and s != strategy:
            continue
        if key and kk != key:
            continue
        if model and sticky[k].get("model") != model:
            continue
        sticky.pop(k, None)
        n += 1
    if n:
        _request_save()
    return n


def sticky_view() -> list:
    """当前粘性表（只读，给 `/api/sticky` 排障用）：哪个会话现在粘在哪个模型上"""
    now = time.time()
    out = []
    for (s, k), v in sticky.items():
        if (v.get("ts") or 0) + STICKY_TTL <= now:
            continue
        out.append({"strategy": s, "session": k, "model": v.get("model"),
                    "channel": v.get("channel"), "label": v.get("label") or "",
                    "idle_s": round(now - (v.get("ts") or now), 1), "ttl_s": int(STICKY_TTL)})
    out.sort(key=lambda x: x["idle_s"])
    return out


def candidates_for_auto(strategy: str, cfg: dict) -> list:
    """给「auto」请求生成候选：**所有当前可用 (模型, 渠道) 组合**，按策略排序。

    注意这里就是 auto 的「切换顺序」——`main.chat_completions` 会从这个列表**逐个尝试**
    直到成功，所以「能用的排前面」在本函数里已经保证：冷却中 / 渠道熔断 / 429 预判 /
    模型级 down 的 (模型,渠道) 全部被过滤掉，列表里不会出现用不了的组合。

    排序规则与界面一致：收藏的模型优先（组内仍按策略综合分），然后才是未收藏模型。
    收藏表按视图分两套（2026-09-20）：`vision` 用 `pinned_vision`（视觉专属收藏），
    其余策略用 `pinned` —— 两表独立，同一个模型在两边要各收藏一次。
    strategy="vision" 时额外只保留「能看图」的模型（Hermes 辅助视觉模型用）。

    ⚠️ 这里给的是**策略顺序**；auto 请求实际先用谁还要再看**会话粘性**
    （`prefer_sticky`：本会话上一次成功产出的模型优先，避免同一对话里来回换模型）。"""
    now = time.time()
    pinned = set(cfg.get("pinned_vision" if strategy == "vision" else "pinned") or [])
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
            # 2026-09-21（用户拍板 A1）：不再读模型级状态。能不能路由只看 (模型,渠道) 自己的
            # 记录 —— cooldown / channel_down / ratelimit 都在上面几行判完了。旧逻辑在这里
            # 用 model_status[模型] 一票否决整个模型，会把该模型其他健康渠道一起踢出候选。
            if preempt and throttle.blocked(ch["id"], m):
                continue
            seen.add(key)
            out.append({"channel": ch, "model": m})

    # 收藏优先（0 < 1），组内按策略综合分从高到低
    out.sort(key=lambda c: (0 if c["model"] in pinned else 1,
                            -_composite(c, cfg, strategy)))
    return out


def prefer_sticky(cands: list, strategy: str, key: str) -> tuple:
    """把本会话「上一次成功服务的模型」提到候选最前 → `(候选, 命中说明或 None)`。

    **优先级高于收藏**（`main.chat_completions` 在 `candidates_for_auto` 之后调用它）。
    粘的是**模型**不是渠道：原来的渠道不可用了就换同模型的另一个渠道继续用，
    免得渠道抖一下就把整个会话换到别的模型上。
    该模型**整个不可用**（所有渠道都在冷却/硬失败/被禁用）→ 删掉这条粘性、退回策略排序，
    下一次成功再重新粘。"""
    ent = sticky.get((strategy, key))
    if not ent:
        return cands, None
    now = time.time()
    if (ent.get("ts") or 0) + STICKY_TTL <= now:
        sticky.pop((strategy, key), None)
        return cands, None
    same_model = [c for c in cands if c["model"] == ent.get("model")]
    if not same_model:
        sticky.pop((strategy, key), None)
        return cands, None
    exact = [c for c in same_model if c["channel"]["id"] == ent.get("channel")]
    head = exact[0] if exact else same_model[0]   # 候选已按策略分排序 → 取该模型最优渠道
    rest = [c for c in cands if c is not head]
    return [head] + rest, {
        "model": ent.get("model"), "channel": head["channel"],
        "idle_s": round(now - (ent.get("ts") or now), 1),
        "switched_channel": not exact,
    }


def list_reserved_auto() -> list:
    return list(RESERVED_AUTO.keys())


def channel_available_models(cid: str) -> int:
    """某渠道「当前可用」的模型数：不在冷却、不在待验证、无该 (模型,渠道) 硬失败、渠道未熔断

    2026-09-21（A1）起不再读模型级状态：这个计数要和真的能路由的路径同口径，而路由已不看
    模型级状态（否则卡片上的「可用 N 个」会比实际能用的少）。"""
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
        avail += 1
    return avail
