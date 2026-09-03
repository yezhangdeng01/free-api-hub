"""滑动窗口限流计数 + 429 自学阈值（预判式换路）

思路（借鉴 FreeLLMAPI 但去掉静态表）：
- 提供方不会告诉你限流上限，所以我们用真实 429 反推"安全水位"：
  每次撞 429 时，统计它发生前 60 秒 / 24 小时内我们实际打过的量，
  把"这个量"记为一次观测，多次观测取最小值并留安全边际。
- 路由前若预计本次会越过水位，就把该 (渠道, 模型) 降权/跳过，
  而不是等被打回 429 再冷却。
- 连续 1 小时没再撞 429 → 丢弃旧学习值（平台可能放宽/收紧，允许重新学习）。
"""
import collections
import threading
import time

_lock = threading.Lock()
# (cid, model) -> deque of (ts, kind)  kind: 1=已发出的调用, 0=撞到的429
_CALLS: dict = collections.defaultdict(lambda: collections.deque(maxlen=4000))
# (cid, model) -> {"rpm": int|None, "rpd": int|None, "samples": int, "learned": float}
_LEARN: dict = {}
# 安全边际：实际打到上限的 ~85% 就提前换路
_MARGIN = 0.85
_MIN_SAMPLES = 2        # 至少 2 次一致观测才启用预判（避免单次抖动误伤）
_FORGET_SEC = 3600      # 1 小时没再撞 429 就遗忘学习值
_RPM_WIN = 60
_RPD_WIN = 86400


def _prune(cid, model, cutoff):
    ev = _CALLS[(cid, model)]
    while ev and ev[0][0] < cutoff:
        ev.popleft()
    if not ev:
        _CALLS.pop((cid, model), None)


def record_call(cid: str, model: str, ts: float = None):
    """每次真正发往上游前调用（计一次'已发出的调用'）"""
    ts = ts or time.time()
    with _lock:
        _CALLS[(cid, model)].append((ts, 1))


def observe_429(cid: str, model: str, ts: float = None):
    """撞到 429 时调用：反推安全水位"""
    ts = ts or time.time()
    with _lock:
        _CALLS[(cid, model)].append((ts, 0))
        ev = _CALLS[(cid, model)]
        # 只统计这次 429 之前、窗口内的调用数
        calls60 = sum(1 for t, k in ev if k == 1 and ts - _RPM_WIN <= t < ts)
        calls24h = sum(1 for t, k in ev if k == 1 and ts - _RPD_WIN <= t < ts)
        pre = _LEARN.setdefault((cid, model),
                                {"rpm": None, "rpd": None, "samples": 0, "learned": ts})
        pre["samples"] += 1
        pre["rpm"] = min(pre["rpm"], calls60) if pre["rpm"] is not None else calls60
        pre["rpd"] = min(pre["rpd"], calls24h) if pre["rpd"] is not None else calls24h
        pre["learned"] = ts
        # 只保留最近窗口内的记录（防 deque 无限）
        _prune(cid, model, ts - _RPD_WIN)


def _active(cid, model, now):
    pre = _LEARN.get((cid, model))
    if not pre or pre["samples"] < _MIN_SAMPLES:
        return False
    if now - pre["learned"] > _FORGET_SEC:
        # 太久没撞 429 → 遗忘，允许重新学习
        return "forget"
    return True


def blocked(cid: str, model: str) -> bool:
    """路由前调用：True 表示建议这次别走这条路（预计会越线）"""
    now = time.time()
    with _lock:
        st = _active(cid, model, now)
        if st == "forget":
            _LEARN.pop((cid, model), None)
            return False
        if not st:
            return False
        pre = _LEARN[(cid, model)]
        ev = _CALLS[(cid, model)]
        _prune(cid, model, now - _RPD_WIN)
        if pre["rpm"] is not None:
            rpm = sum(1 for t, k in ev if k == 1 and now - _RPM_WIN <= t <= now)
            if rpm + 1 > max(2, int(pre["rpm"] * _MARGIN)):
                return True
        if pre["rpd"] is not None:
            rpd = sum(1 for t, k in ev if k == 1 and now - _RPD_WIN <= t <= now)
            if rpd + 1 > max(5, int(pre["rpd"] * _MARGIN)):
                return True
    return False


def observed(cid: str, model: str) -> dict:
    """查看学习状态（调试/日志用）"""
    with _lock:
        pre = _LEARN.get((cid, model))
        if not pre:
            return {}
        return {k: v for k, v in pre.items() if k != "samples"}
