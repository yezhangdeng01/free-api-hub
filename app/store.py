"""SQLite 用量统计 + 模型状态持久化"""
import json
import os
import sqlite3
import sys
import threading
import time

if getattr(sys, "frozen", False):
    # PyInstaller 打包（绿色版）：数据目录在 exe 同目录
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "usage.db")
MODEL_STATUS_PATH = os.path.join(ROOT, "data", "model_status.json")
RUNTIME_STATE_PATH = os.path.join(ROOT, "data", "runtime_state.json")
CAPABILITY_CACHE_PATH = os.path.join(ROOT, "data", "capability_cache.json")
_lock = threading.Lock()
_ms_cache = None


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _conn() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            channel_id TEXT,
            channel_name TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            success INTEGER DEFAULT 1,
            error TEXT,
            upstream_model TEXT DEFAULT '',
            cancelled INTEGER DEFAULT 0,
            superseded INTEGER DEFAULT 0,
            out_chars INTEGER DEFAULT 0
        )""")
        # 兼容老库：缺列就补（PUT request 实际命中模型，用于 auto* 路由的归因）
        cols = [r[1] for r in con.execute("PRAGMA table_info(usage)").fetchall()]
        if "upstream_model" not in cols:
            con.execute("ALTER TABLE usage ADD COLUMN upstream_model TEXT DEFAULT ''")
        # cancelled：客户端主动断开（用户点停止 / 客户端超时）。上游没错，不该算失败
        if "cancelled" not in cols:
            con.execute("ALTER TABLE usage ADD COLUMN cancelled INTEGER DEFAULT 0")
        # superseded：这次尝试失败后网关**又换了下一个渠道继续**，客户端最终拿到了结果
        # （候选循环里除最后一条外的失败都属此类）。它不是客户端可见的失败，统计里不算错
        if "superseded" not in cols:
            con.execute("ALTER TABLE usage ADD COLUMN superseded INTEGER DEFAULT 0")
        # out_chars：流式响应里模型实际吐出的**内容字符数**。用途只有一个：客户端提前断开时
        # 上游的 usage 收尾块收不到 → token 数拿不到，用它证明「模型确实干活了」。
        # 别拿它换算 token（中英混排没有稳定比例），所以单独存一列、界面也照实标「字」。
        if "out_chars" not in cols:
            con.execute("ALTER TABLE usage ADD COLUMN out_chars INTEGER DEFAULT 0")
            # 老行回填：中断那行的备注里本来就写着字符数（「客户端中断（模型已正常输出 17374
            # 字符后断开）」），趁这次加列把数字抠进新列 —— 只跑一次（列刚加上时），
            # 否则每次启动都全表扫。回填不到的行保持 0，界面照旧显示「—」。
            import re as _re_chars
            for rid, err in con.execute(
                    "SELECT id, error FROM usage WHERE COALESCE(out_chars, 0) = 0"
                    " AND error LIKE '客户端中断（模型已正常输出%'").fetchall():
                m = _re_chars.search(r"已正常输出 (\d+) 字符", err or "")
                if m:
                    con.execute("UPDATE usage SET out_chars = ? WHERE id = ?",
                                (int(m.group(1)), rid))


# ---------------- 模型可用性状态持久化（重启不丢） ----------------
def load_model_status() -> dict:
    global _ms_cache
    if _ms_cache is None:
        try:
            with open(MODEL_STATUS_PATH, "r", encoding="utf-8") as f:
                _ms_cache = json.load(f)
        except Exception:
            _ms_cache = {}
    return _ms_cache


def _atomic_write(path: str, data):
    """临时文件 + os.replace 原子替换，避免写盘中断留下损坏的半截 JSON"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def persist_model_status(model: str, entry: dict):
    """写一条模型状态到磁盘（每次 mark_model_status 后调用）"""
    global _ms_cache
    d = load_model_status()
    d[model] = entry
    with _lock:
        _atomic_write(MODEL_STATUS_PATH, d)


# ---------------- 网关运行时状态持久化（冷却/待验证/渠道级硬失败，重启不丢） ----------------
def load_runtime_state() -> dict:
    try:
        with open(RUNTIME_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def persist_runtime_state(data: dict):
    with _lock:
        _atomic_write(RUNTIME_STATE_PATH, data)


# ---------------- 能力榜单缓存（AA 榜分 / 视觉能力，OpenRouter 断供也不清零） ----------------
def load_capability_cache() -> dict:
    try:
        with open(CAPABILITY_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def persist_capability_cache(data: dict):
    with _lock:
        _atomic_write(CAPABILITY_CACHE_PATH, data)


def model_usage_24h() -> dict:
    """近 24h 每个模型的调用统计 {model: {requests, ok, avg_latency}}

    模型名优先取 `upstream_model`（真实命中的上游模型），没有才退回 `model`。
    否则走 `auto-*` 别名路由的调用会全记到别名头上，而 /api/overview 是按真实模型
    id 去查的，取不到 → 模型列表里「24h N 次」标签恒不显示。
    口径与 `summary().by_model`、`recent_outcomes()` 保持一致。"""
    since = time.time() - 86400
    with _conn() as con:
        rows = con.execute(
            "SELECT COALESCE(NULLIF(upstream_model, ''), model) AS m, COUNT(*) AS n,"
            " COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS ok,"
            " COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg"
            " FROM usage WHERE ts >= ? GROUP BY 1", (since,)).fetchall()
    return {r[0]: {"requests": r[1], "ok": r[2], "avg_latency": int(r[3] or 0)} for r in rows}


def log_usage(channel_id, channel_name, model, prompt_tokens, completion_tokens,
              latency_ms, success, error=None, upstream_model="", cancelled=False,
              superseded=False, out_chars=0):
    with _lock, _conn() as con:
        con.execute(
            "INSERT INTO usage (ts, channel_id, channel_name, model, prompt_tokens,"
            " completion_tokens, latency_ms, success, error, upstream_model, cancelled,"
            " superseded, out_chars)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), channel_id, channel_name, model,
             prompt_tokens or 0, completion_tokens or 0, latency_ms or 0,
             1 if success else 0, error or None, upstream_model or "",
             1 if cancelled else 0, 1 if superseded else 0, out_chars or 0))


def recent_outcomes(days: int = 120, per_pair: int = 10) -> list:
    """每个 (上游模型, 渠道) 最近 per_pair 次的成败（新→旧），供回填稳定分窗口用。

    模型名优先取 `upstream_model`（真实上游名，与稳定分的 key 一致），没有才退回 `model`。
    用窗口函数在 SQL 里取「每组最近 N 条」，Python 侧按顺序取即可。
    返回元组最后一位是 `cancelled`：客户端主动中断（用户点停止/客户端超时）不是模型的账，
    回填时必须跳过，否则会把它当成一次失败灌进稳定分窗口。"""
    since = time.time() - days * 86400
    with _conn() as con:
        return con.execute(
            "SELECT m, channel_id, success, COALESCE(error, ''), ts, cancelled FROM ("
            "  SELECT COALESCE(NULLIF(upstream_model, ''), model) AS m, channel_id, success,"
            "         error, ts, COALESCE(cancelled, 0) AS cancelled, ROW_NUMBER() OVER ("
            "           PARTITION BY COALESCE(NULLIF(upstream_model, ''), model), channel_id"
            "           ORDER BY ts DESC) AS rn"
            "  FROM usage WHERE ts >= ?)"
            " WHERE rn <= ? ORDER BY m, channel_id, ts DESC", (since, per_pair)).fetchall()


def distinct_models(days: int = 7) -> list:
    """近期成功调用过的模型（用于主动探测，把探测成本压在真实使用过的模型上）

    同样优先取 `upstream_model`：探测逻辑要拿真实模型名去各渠道的 `cs.models` 里比对，
    如果返回的是 `auto-*` 别名，会被当成「该渠道没这个模型」整批跳过，主动探测空转。"""
    since = time.time() - days * 86400
    with _conn() as con:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT COALESCE(NULLIF(upstream_model, ''), model) FROM usage"
            " WHERE ts >= ? AND success = 1", (since,))]


def recent(limit: int = 50, offset: int = 0) -> dict:
    """最近的请求明细（倒序）"""
    with _conn() as con:
        logs = [dict(r) for r in con.execute(
            "SELECT id, ts, channel_name, model, upstream_model,"
            " prompt_tokens, completion_tokens, latency_ms, success, error,"
            " COALESCE(cancelled, 0) AS cancelled,"
            " COALESCE(superseded, 0) AS superseded,"
            " COALESCE(out_chars, 0) AS out_chars"
            " FROM usage ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset))]
    return {"logs": logs}


def summary(days: int = 7) -> dict:
    """请求统计（**按「模型×尝试」记的**，不是按客户端请求记的）。

    一行 = 向上游某个模型发出去的一次尝试。所以这一跳失败就是失败，**不因为「后面换渠道成功了」
    而豁免**（用户 2026-09-17 明确的统计口径：「我们统计的就是具体的模型是失败还是成功还是其它什么」）。

    - `errors`：这一跳失败（`success=0`）且不是客户端主动中断 —— **含换路那几次**；
    - `cancelled`：客户端主动中断（用户点停止 / 客户端超时），上游其实是正常的 → 不算失败；
    - `superseded`：**补充信息**，指这些失败里有几次后面还换到了别的候选（客户端最终拿到了结果）。
      它不改变失败归属，只是让你知道「这一跳失败之后请求续到了哪」。
    - 成功率分母用 `requests - cancelled`（中断那一跳不是模型的账）。"""
    since = time.time() - days * 86400
    _ERR = "(success = 0 AND COALESCE(cancelled,0) = 0)"       # 含 superseded
    with _conn() as con:
        totals = dict(con.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            f" COALESCE(SUM(CASE WHEN {_ERR} THEN 1 ELSE 0 END), 0) AS errors,"
            " COALESCE(SUM(COALESCE(cancelled, 0)), 0) AS cancelled,"
            " COALESCE(SUM(COALESCE(superseded, 0)), 0) AS superseded,"
            " COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg_latency"
            " FROM usage WHERE ts >= ?", (since,)).fetchone())
        daily = [dict(r) for r in con.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS date, COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            f" COALESCE(SUM(CASE WHEN {_ERR} THEN 1 ELSE 0 END), 0) AS errors,"
            " COALESCE(SUM(COALESCE(cancelled, 0)), 0) AS cancelled,"
            " COALESCE(SUM(COALESCE(superseded, 0)), 0) AS superseded"
            " FROM usage WHERE ts >= ? GROUP BY date ORDER BY date", (since,))]
        by_model = [dict(r) for r in con.execute(
            "SELECT COALESCE(NULLIF(upstream_model, ''), model) AS model,"
            " COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg_latency"
            " FROM usage WHERE ts >= ? GROUP BY 1 ORDER BY requests DESC LIMIT 20", (since,))]
        by_channel = [dict(r) for r in con.execute(
            "SELECT COALESCE(channel_name, channel_id) AS name, COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            f" COALESCE(SUM(CASE WHEN {_ERR} THEN 1 ELSE 0 END), 0) AS errors"
            " FROM usage WHERE ts >= ? GROUP BY channel_id ORDER BY requests DESC", (since,))]
    return {"totals": totals, "daily": daily, "by_model": by_model, "by_channel": by_channel}
