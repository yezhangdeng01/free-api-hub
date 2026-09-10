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
            upstream_model TEXT DEFAULT ''
        )""")
        # 兼容老库：缺列就补（PUT request 实际命中模型，用于 auto* 路由的归因）
        cols = [r[1] for r in con.execute("PRAGMA table_info(usage)").fetchall()]
        if "upstream_model" not in cols:
            con.execute("ALTER TABLE usage ADD COLUMN upstream_model TEXT DEFAULT ''")


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


def model_usage_24h() -> dict:
    """近 24h 每个模型的调用统计 {model: {requests, ok, avg_latency}}"""
    since = time.time() - 86400
    with _conn() as con:
        rows = con.execute(
            "SELECT model, COUNT(*) AS n,"
            " COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS ok,"
            " COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg"
            " FROM usage WHERE ts >= ? GROUP BY model", (since,)).fetchall()
    return {r[0]: {"requests": r[1], "ok": r[2], "avg_latency": int(r[3] or 0)} for r in rows}


def log_usage(channel_id, channel_name, model, prompt_tokens, completion_tokens,
              latency_ms, success, error=None, upstream_model=""):
    with _lock, _conn() as con:
        con.execute(
            "INSERT INTO usage (ts, channel_id, channel_name, model, prompt_tokens,"
            " completion_tokens, latency_ms, success, error, upstream_model) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), channel_id, channel_name, model,
             prompt_tokens or 0, completion_tokens or 0, latency_ms or 0,
             1 if success else 0, error or None, upstream_model or ""))


def distinct_models(days: int = 7) -> list:
    """近期成功调用过的模型（用于主动探测，把探测成本压在真实使用过的模型上）"""
    since = time.time() - days * 86400
    with _conn() as con:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT model FROM usage WHERE ts >= ? AND success = 1", (since,))]


def recent(limit: int = 50, offset: int = 0) -> dict:
    """最近的请求明细（倒序）"""
    with _conn() as con:
        logs = [dict(r) for r in con.execute(
            "SELECT id, ts, channel_name, model, upstream_model,"
            " prompt_tokens, completion_tokens, latency_ms, success, error"
            " FROM usage ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset))]
    return {"logs": logs}


def summary(days: int = 7) -> dict:
    since = time.time() - days * 86400
    with _conn() as con:
        totals = dict(con.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS errors,"
            " COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg_latency"
            " FROM usage WHERE ts >= ?", (since,)).fetchone())
        daily = [dict(r) for r in con.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS date, COUNT(*) AS requests,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS errors"
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
            " COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS errors"
            " FROM usage WHERE ts >= ? GROUP BY channel_id ORDER BY requests DESC", (since,))]
    return {"totals": totals, "daily": daily, "by_model": by_model, "by_channel": by_channel}
