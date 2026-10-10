"""OpenRouter「免费模型雷达」：把 /models 目录里**真免费**的模型收下来，
记住每个模型**本地第一次见到**的时间，好在界面上提示「有新面孔上架」。

为什么单开一个模块，而不是塞进 capability：
  · 判据在 providers（`_is_zero_pricing`：免费与否只看 pricing，不看名字后缀）；
  · 但「什么时候第一次见到它」是**跨重启要活下来**的状态 —— 它决定界面上那个「新」标记。
    这一份只有本地才知道（OpenRouter 的 `created` 是模型在站内的上线时间，跟「我什么时候
    才知道它」不是一回事：我这台机器可能十天没联网，一次补回来五个「新面孔」）。
  · 而且它跟榜分不一样：榜分是**只增不减、不设过期**的权威数据；免费名单是**快照**，
    OpenRouter 随时会把免费模型转成收费或下架，所以这里允许条目「失活」。

数据源两条，谁先到谁喂（都走 `providers`，这里不关心来源）：
  ① 渠道健康检查拉 OpenRouter `/models` —— 配了渠道且有 Key 的用户走这条；
  ② 免密钥公开端点（`providers.fetch_openrouter_public`）—— 没配渠道 / Key 失效也照有情报。

⚠️ 与「渠道里能用的模型」不是一回事：这里收的是 **OpenRouter 全站目录**里价格全 0 的模型，
   跟本机配了哪些渠道无关。所以界面上要讲清楚「要用得先在渠道页配一个 OpenRouter」。
"""
import logging
import time

logger = logging.getLogger("api-hub")

_CACHE_SCHEMA = 1
# 默认「新面孔」窗口（天）：第一次见到距今在这个窗口内 → 界面上打「新」标
NEW_DAYS = 7
# 失活条目（本次抓取没出现）保留多久再清掉。留着是为了「下架又回来」时仍算老面孔，
# 不是因为还要展示它 —— 太长时间不清理，这个文件会变成 OpenRouter 的历史坟场。
GONE_KEEP_DAYS = 120

_items = {}        # id -> 明细 dict（含 active 标记）
_first_seen = {}   # id -> 本地第一次见到的时间戳
_ts = 0.0          # 最近一次成功抓到的时间（0 = 从来没成功过）
_dirty = {"v": False}


def _feed_tiers():
    """把「匿名测试模型」名单推给 capability（那批默认走智能档，见 `capability.set_anon_models`）。

    抓取（`update`）与重启恢复（`load_cache`）**两条路都要推** —— 少推一条就会出现
    「重启前是智能档、重启后变轻量档」这种看起来像 bug 的现象。

    为什么只推 `kind == "anon"`：`:free` 后缀那批名字里带厂商前缀（`z-ai/glm-5.3-flash:free`），
    名字启发式认得出家族；匿名测试模型恰恰是**什么都没有**的那批，才需要单独兜一条。
    """
    from . import capability
    ids = {m for m, r in _items.items() if (r.get("kind") or "") == "anon"}
    n = capability.set_anon_models(ids)
    if n:
        logger.info("匿名测试模型 %d 个 → 默认智能档：%s", n, "、".join(sorted(ids)))
    return n


def update(entries, ts: float = None) -> dict:
    """合并一次抓取结果，返回 {"total": 在架数, "added": [本次新见到的 id], "gone": 失活数}。

    `entries` 来自 `providers.free_model_entries()`（已按 pricing 判过免费）。
    合并规则：
      · 本次出现的 → 明细整条覆盖（名字/上下文/榜分会变），`active=True`，首次见到时间保留；
      · 本次**没**出现的 → `active=False`（不是删掉：OpenRouter 时常「免费模型转付费后又放回来」，
        删了的话它回来时会被当成新面孔又提示一次，噪音）。
    """
    global _ts
    now = time.time() if ts is None else float(ts)
    seen, added = set(), []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        mid = str(e.get("id") or "").strip()
        if not mid:
            continue
        seen.add(mid)
        is_new_to_us = mid not in _items
        row = dict(e)
        row["active"] = True
        _items[mid] = row
        # setdefault 而不是赋值：命中已有条目时**必须保留**原来的首次见到时间，
        # 否则每抓一次，所有老模型都会变成「今天刚发现」——「新面孔」提示就永远在闪。
        if _first_seen.setdefault(mid, now) == now and is_new_to_us:
            added.append(mid)
    gone = 0
    for mid, row in _items.items():
        if mid not in seen and row.get("active"):
            row["active"] = False
        if not row.get("active"):
            gone += 1
    _gc(now)
    _ts = now
    _dirty["v"] = True
    _feed_tiers()          # 名单变了 → 立刻把「匿名测试模型」名单同步给定档逻辑
    if added:
        logger.info("免费模型雷达：本次新见到 %d 个（在架共 %d 个）", len(added), len(seen))
    return {"total": len(seen), "added": added, "gone": gone}


def _gc(now: float):
    """清掉长期失活的历史条目（判据用首次见到时间，不是失活时间 —— 不为它再多存一个字段）"""
    limit = now - GONE_KEEP_DAYS * 86400
    for mid in [m for m, r in _items.items()
                if not r.get("active") and _first_seen.get(m, 0) < limit]:
        _items.pop(mid, None)
        _first_seen.pop(mid, None)


def cache_age_hours():
    """距上次成功抓取多少小时；从没抓过返回 None（调用方据此决定要不要去抓）"""
    return None if not _ts else (time.time() - _ts) / 3600.0


def view(new_days: int = None) -> dict:
    """给 /api/overview 用的视图：在架清单 + 新面孔计数（含 `is_new` 标记）。

    只列**在架**的；已下架的只报个数（界面上一句「另有 N 个已下架」足够，不必逐条展示）。
    """
    nd = NEW_DAYS if new_days is None else max(1, int(new_days))
    now = time.time()
    cutoff = now - nd * 86400
    items, kinds = [], {"free": 0, "anon": 0, "router": 0}
    for mid, row in _items.items():
        if not row.get("active"):
            continue
        kind = row.get("kind") or "free"
        kinds[kind] = kinds.get(kind, 0) + 1
        first = _first_seen.get(mid, 0.0)
        r = dict(row)
        r["first_seen"] = first
        r["is_new"] = bool(first and first >= cutoff)
        items.append(r)
    # 排序：新面孔优先 → 有榜分的按分降序 → 其余按 id。理由：「提示」的价值在新增，
    # 而榜单分高的免费模型是真正值得先去试的那批（低分的免费模型满地都是）。
    items.sort(key=lambda r: (not r["is_new"], -(r["bench"] or 0), r["id"]))
    new_count = sum(1 for r in items if r["is_new"])
    return {
        "ts": _ts,
        "age_hours": cache_age_hours(),
        "total": len(items),
        "new_count": new_count,
        "new_days": nd,
        "kinds": kinds,
        "gone": sum(1 for r in _items.values() if not r.get("active")),
        "never": _ts == 0.0,
        "items": items,
    }


# ---- 缓存读写（调用方：启动时 load_cache，后台循环里 save_cache） ----
def load_cache() -> dict:
    """启动时从磁盘恢复免费名单 —— 上次抓到的东西不该因为重启就消失"""
    global _ts
    from . import store
    data = store.load_free_models_cache() or {}
    if int(data.get("schema") or 0) != _CACHE_SCHEMA:
        return {"loaded": False, "total": 0}
    _items.clear()
    _first_seen.clear()
    for mid, row in (data.get("items") or {}).items():
        if isinstance(row, dict):
            _items[str(mid)] = row
    for mid, ts in (data.get("first_seen") or {}).items():
        try:
            _first_seen[str(mid)] = float(ts)
        except (TypeError, ValueError):
            continue
    _ts = float(data.get("ts") or 0)
    _dirty["v"] = False      # 刚读回来的东西没必要原样写回去
    _feed_tiers()            # 重启恢复后也要推一次，否则档位退回名字启发式
    active = sum(1 for r in _items.values() if r.get("active"))
    return {"loaded": bool(_items), "total": active, "ts": _ts}


def save_cache(force: bool = False) -> bool:
    """落盘（脏了才写；force 用于停机收尾）"""
    from . import store
    if not force and not _dirty["v"]:
        return False
    store.persist_free_models_cache({
        "schema": _CACHE_SCHEMA,
        "ts": _ts,
        "items": _items,
        "first_seen": _first_seen,
    })
    _dirty["v"] = False
    return True
