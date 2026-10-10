"""各平台模型列表与额度查询"""
import logging

import httpx

from app import capability, freemodels

logger = logging.getLogger("api-hub")

# 智谱网页端额度接口（社区逆向，非官方文档，失败会自动降级）
ZHIPU_BALANCE_URL = "https://www.bigmodel.cn/api/biz/account/query-customer-account-report"
# OpenRouter 公开模型目录（**无需密钥**）：作为 AA 榜分/视觉能力的兜底数据源
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def _is_zero_pricing(it: dict) -> bool:
    """该 /models 项是否「价格全 0」（真免费）。

    为什么不能只看名字后缀（2026-09-28 实测）：OpenRouter 的**匿名 stealth 预览模型**
    （如 `stealth/space-bunny-alpha`）pricing 是 `{"prompt": "0", "completion": "0"}`，
    但 id **不带** `:free` 后缀 —— 只看后缀会把它连同 440 个付费模型一起丢掉。"""
    p = it.get("pricing") or {}
    try:
        return float(p.get("prompt") or 0) == 0 and float(p.get("completion") or 0) == 0
    except (TypeError, ValueError):
        return False


def _is_text_only_output(it: dict) -> bool:
    """输出模态是否**只有 text**（才配进 chat 列表）。缺字段按「是」处理（宁松勿误杀）。

    挡掉 `google/lyria-*` 这类**音频生成**模型：它们的 output_modalities 是
    `["text", "audio"]`（含 text，所以「含 text」这种松判据挡不住），只有「仅 text」才挡得住。"""
    outs = (it.get("architecture") or {}).get("output_modalities")
    if not outs:
        return True
    outs = [outs] if isinstance(outs, str) else outs
    return all("text" in str(x).lower() for x in outs)


def _has_image_input(it: dict):
    """能否读图（`architecture.input_modalities` 里有 image）。**不知道时返回 None**。

    为什么不给「缺字段」一个默认值：`_harvest` 只在字段存在时才敢给模型贴
    「支持/不支持视觉」的标签（没标签 = 未知，档位判定会退回名字启发式）；
    而免费模型清单里 `bool(None)` 自然就是「不显示 👁」。两边要的默认值不同，
    所以这里返回三态，由调用方各自决定。"""
    arch = it.get("architecture") or {}
    mods = arch.get("input_modalities") or arch.get("modality")
    if not mods:
        return None
    mods = [mods] if isinstance(mods, str) else mods
    return "image" in " ".join(str(x).lower() for x in mods)


def _harvest(items) -> tuple:
    """从 /models 的返回项里收割「AA 榜分 + 视觉能力 + 价格为 0 的模型」（OpenRouter 系接口才带）

    返回 (ids, bench, vision_ok, vision_no, free_by_price)，并顺手落库到 capability。
    `free_by_price` 是 pricing 全 0 且**输出仅 text** 的 id 小写集合 —— 供 `filter_free_models`
    判断「不看后缀也真的免费」的模型（stealth 预览模型走这条）。
    """
    ids, bench = [], {}
    vision_ok, vision_no = set(), set()
    free_by_price = set()
    for it in items or []:
        if not isinstance(it, dict):
            if it:
                ids.append(str(it))
            continue
        mid = it.get("id")
        if not mid:
            continue
        ids.append(mid)
        if _is_zero_pricing(it) and _is_text_only_output(it):
            free_by_price.add(str(mid).strip().lower())
        aa = ((it.get("benchmarks") or {}).get("artificial_analysis") or {})
        v = aa.get("intelligence_index")
        if isinstance(v, (int, float)):
            bench[capability.norm_id(mid)] = v
        mods = _has_image_input(it)
        if mods is not None:
            (vision_ok if mods else vision_no).add(capability.norm_id(mid))
    if bench:
        info = capability.update_bench_scores(bench)
        logger.info("收割 AA 榜分 %d 个（累计 %d，智能阈值 %.1f / 中档阈值 %.1f）",
                    len(bench), info["n"], info["hi"], info["mid"])
    if vision_ok or vision_no:
        vin = capability.update_vision_models(vision_ok, vision_no)
        logger.info("收割视觉能力 %d 支持 / %d 不支持（累计 %d / %d）",
                    len(vision_ok), len(vision_no), vin["ok"], vin["no"])
    return ids, bench, vision_ok, vision_no, free_by_price


# ---- OpenRouter：只保留免费模型 ----
# 为什么必须在**拉列表**这一步就过滤（2026-09-26 实测，2026-09-28 修补判据）：
#   · OpenRouter 的 `/models` 返回的是**全站目录**，实测 458 个；
#   · 其中真正能免费调的是「名字带 `:free` 后缀」+ `openrouter/free`（官方免费模型路由器）
#     + **价格全 0 但名字不带后缀的模型**（2026-09-28 补）—— 其余一律要付费/绑卡。
#   · 用户侧表现是「可用模型里多了很多 OpenRouter 的实际不可用模型，显示可用，测试就不可用」——
#     因为「能不能用」只看 (模型,渠道) 级记录（见 gateway.model_view），而没测过的模型
#     没有任何记录 → 渠道健康就显示「可用」；点一次测试才追加一条 `channel_down`，
#     而且要先花一次 `endpoints` 请求去确认它没有免费提供方。
# 过滤后 458 → 约 19：那一整类「看着可用、一测就 402」的模型从根上消失，顺带省掉 400+ 次探测
# 与 400+ 次 endpoints 查询。
#
# ⚠️ 判据红线（2026-09-28）：**别只看名字后缀**。OpenRouter 的**匿名 stealth 预览模型**
#    （如 `stealth/space-bunny-alpha`，2026-09-23 上线）pricing 是 0 却不带 `:free` 后缀，
#    只看后缀会把它们当付费模型误杀。免费与否只有 `pricing` 说了算
#    （辅助集合由 `_harvest` 看全量 items 时顺手收，零额外请求）。
# 想恢复列出付费模型（比如以后给 OpenRouter 充了值）：给该渠道加 `"free_only": false`。
OPENROUTER_FREE_ONLY = True
_OR_FREE_EXTRA = {"openrouter/free"}   # 不带 `:free` 后缀但确实免费（官方免费模型路由器）


def is_free_or_model(mid: str, free_ids=None) -> bool:
    """该 OpenRouter 模型 id 是否属于「免费可调」。

    两条路：① 名字带 `:free` 后缀 / 命中 `_OR_FREE_EXTRA` 白名单；
    ② 命中 `free_ids`（pricing 全 0 的 id 集合，小写）—— 专治 stealth 预览模型这类
    「免费但名字不带后缀」的漏网之鱼。"""
    s = (mid or "").strip().lower()
    if s.endswith(":free") or s in _OR_FREE_EXTRA:
        return True
    return bool(free_ids) and s in free_ids


def filter_free_models(ids, ch_type: str = None, free_only=None, free_ids=None) -> tuple:
    """按渠道类型过滤模型列表 → `(保留的 ids, 被丢掉的个数)`。

    目前只有 openrouter 需要过滤：其余平台的 `/models` 返回的就是这个 Key 真正可调的模型，
    过滤会误删。`free_only=None` 时用 `OPENROUTER_FREE_ONLY`。
    `free_ids` = pricing 全 0 的 id 集合（小写），由 `fetch_models` 从 `_harvest` 拿。"""
    if (ch_type or "") != "openrouter":
        return list(ids), 0
    if free_only is None:
        free_only = OPENROUTER_FREE_ONLY
    if not free_only:
        return list(ids), 0
    keep = [i for i in ids if is_free_or_model(i, free_ids)]
    return keep, len(ids) - len(keep)


def free_model_entries(items) -> list:
    """从 /models 返回项里抽出**所有免费模型**的明细（喂 `app.freemodels` 雷达）。

    与 `filter_free_models` 同源判据（`_is_zero_pricing` + `_is_text_only_output`），
    所以雷达里列出来的，就是渠道里真正会被保留的那批 —— 不会出现「提示有、列表里没有」的错位。

    `kind` 三分类只为**界面措辞**服务，不影响「免费」这个事实：
      · `free`   —— 带 `:free` 后缀的常规免费版（OpenRouter 按天限次，最稳）；
      · `router` —— `openrouter/free`，官方免费模型路由器（它自己挑一个免费模型来答）；
      · `anon`   —— **匿名 stealth 预览模型**之类：pricing 全 0 却不带后缀，
                    往往是新模型正式发布前放出来的灰度测试版（2026-09-23 出现过
                    `stealth/space-bunny-alpha`）。这类最值得提示，因为**过一阵就没了**。

    ⚠️ **只能在 OpenRouter 系接口的返回上调用**。别的平台 `/models` 根本不返回 `pricing`，
    而 `_is_zero_pricing` 对缺字段是「缺 = 0 = 免费」的宽容判据（宁松勿误杀那批 stealth 模型），
    拿它们来喂会把几百个付费模型全标成免费。调用点已按 ch_type 把关（见 fetch_models）。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        if not (_is_zero_pricing(it) and _is_text_only_output(it)):
            continue
        mid = str(it["id"]).strip()
        low = mid.lower()
        if low.endswith(":free"):
            kind = "free"
        elif low in _OR_FREE_EXTRA:
            kind = "router"
        else:
            kind = "anon"
        aa = ((it.get("benchmarks") or {}).get("artificial_analysis") or {})
        v = aa.get("intelligence_index")
        out.append({
            "id": mid,
            "name": it.get("name") or mid,
            "context": it.get("context_length") or 0,
            "kind": kind,
            "created": it.get("created") or 0,
            "bench": v if isinstance(v, (int, float)) else None,
            "vision": bool(_has_image_input(it)),
        })
    return out


async def fetch_models(client: httpx.AsyncClient, base_url: str, api_key: str,
                       ch_type: str = None, free_only=None) -> list:
    """拉取渠道的模型列表（OpenAI 兼容 /models）

    顺带收割两样白拿的权威数据（OpenRouter 的返回里带，不用额外密钥、不多发请求）：

    1. `benchmarks.artificial_analysis.intelligence_index` — Artificial Analysis 智能指数；
    2. `architecture.input_modalities` — **是否支持图像输入**（比名字猜准得多）。

    `ch_type`/`free_only` 只影响**返回的 id 列表**（OpenRouter 只留免费的，见上面）；
    收割在看全量 `items` 时完成，所以过滤不会让榜分/视觉数据少收。"""
    r = await client.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else data
    ids, _bench, _ok, _no, free_by_price = _harvest(items)
    # 免费模型雷达：**在过滤之前**喂（雷达要的是全站目录里的免费模型，与这个渠道
    # 是否 `free_only` 无关）。只有 OpenRouter 系接口才带 pricing，见 free_model_entries。
    if (ch_type or "") == "openrouter":
        entries = free_model_entries(items)
        if entries:
            freemodels.update(entries)
    ids = sorted(set(ids))
    ids, dropped = filter_free_models(ids, ch_type, free_only, free_by_price)
    if dropped:
        logger.info("渠道[%s] 只保留免费模型：%d → %d 个（丢掉 %d 个需付费的）",
                    ch_type, len(ids) + dropped, len(ids), dropped)
    return ids


async def fetch_bench_public(client: httpx.AsyncClient) -> dict:
    """兜底榜分源：直连 OpenRouter **公开** /models（不传密钥）收割 AA 榜分与视觉能力。

    为什么需要它：榜分只藏在 OpenRouter 的 /models 里，渠道一被停用 / Key 失效 / 上游故障，
    数据就断供，档位退回名字启发式（表现就是「评分没了」）。而 AA 智能指数是月级更新的，
    没必要每次刷新都去拿 —— 只在本地缓存过期时来补一次，失败就继续用旧分。
    实测（2026-09-15）：公开端点免密钥即返回 445 个模型，139 个带 AA 榜分（比带密钥的渠道
    返回还多），且全部带 `architecture.input_modalities`。

    2026-10-07 起顺带喂 `freemodels` 雷达：同一份返回里就带着每个模型的 pricing，
    **零额外请求**就能拿到免费名单 —— 没配 OpenRouter 渠道、或 Key 失效的用户也照样有情报
    （这正是「公开端点」存在的意义：不依赖任何 Key）。
    返回 {"bench": n, "vision": n, "free": n, "ok": bool}。
    """
    try:
        r = await client.get(OPENROUTER_MODELS_URL, timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("公开榜分源不可用（继续用本地缓存）: %s", str(e)[:160])
        return {"bench": 0, "vision": 0, "free": 0, "ok": False}
    items = data.get("data", []) if isinstance(data, dict) else data
    _, bench, vision_ok, vision_no, _free = _harvest(items)
    entries = free_model_entries(items)
    if entries:
        freemodels.update(entries)
    # ok 的口径 = 「这趟真拿到东西了」。补进免费名单也算拿到（哪怕榜分这次全空）——
    # 否则 _topup 只报「失败」，而实际上免费雷达已经更新了，日志会误导排查。
    return {"bench": len(bench), "vision": len(vision_ok) + len(vision_no),
            "free": len(entries), "ok": bool(bench or vision_ok or entries)}


async def check_quota(client: httpx.AsyncClient, channel: dict):
    """查询渠道额度。返回规范化 dict 或 None（该平台不支持）"""
    t = channel["type"]
    key = channel["api_key"]
    try:
        if t == "openrouter":
            return await _openrouter(client, key)
        if t == "zhipu":
            return await _zhipu(client, key)
        if t == "siliconflow":
            return await _siliconflow(client, key)
    except Exception as e:
        return {"kind": "error", "label": "查询失败", "detail": str(e)[:160]}
    return None


async def _siliconflow(client: httpx.AsyncClient, key: str):
    """硅基流动 /v1/user/info 返回 data.balance（CNY）"""
    r = await client.get("https://api.siliconflow.cn/v1/user/info",
                         headers={"Authorization": f"Bearer {key}"}, timeout=25)
    if r.status_code != 200:
        return None
    d = r.json().get("data", {}) or {}
    bal = _num(d.get("balance"))
    if bal is None:
        return None
    return {"kind": "balance", "label": "账户余额", "remaining": bal,
            "total": _num(d.get("totalBalance")), "used": _num(d.get("chargeBalance")),
            "unit": "CNY"}


async def _openrouter(client: httpx.AsyncClient, key: str) -> dict:
    h = {"Authorization": f"Bearer {key}"}
    # 优先官方 /credits（需要管理钥匙），失败降级到 /auth/key
    r = await client.get("https://openrouter.ai/api/v1/credits", headers=h, timeout=25)
    if r.status_code == 200:
        d = r.json().get("data", {})
        total = _num(d.get("total_credits")) or 0.0
        used = _num(d.get("total_usage")) or 0.0
        return {"kind": "credits", "label": "账户余额", "total": total, "used": used,
                "remaining": round(total - used, 4), "unit": "USD"}
    r2 = await client.get("https://openrouter.ai/api/v1/auth/key", headers=h, timeout=25)
    r2.raise_for_status()
    d = r2.json().get("data", {})
    usage = _num(d.get("usage")) or 0.0
    limit = _num(d.get("limit"))
    out = {"kind": "keylimit", "label": "Key 用量", "used": usage,
           "limit": limit, "unit": "USD", "free_tier": bool(d.get("is_free_tier"))}
    if limit is not None:
        out["remaining"] = round(limit - usage, 4)
    return out


async def _zhipu(client: httpx.AsyncClient, key: str):
    r = await client.get(ZHIPU_BALANCE_URL,
                         headers={"Authorization": key, "Content-Type": "application/json"},
                         timeout=25)
    if r.status_code != 200:
        return None
    j = r.json()
    avail = _find(j, "availableBalance")
    if avail is None:
        return None
    return {"kind": "balance", "label": "账户余额", "remaining": _num(avail),
            "total": _num(_find(j, "rechargeAmount")),
            "used": _num(_find(j, "totalSpendAmount")), "unit": "CNY"}


def _find(obj, key):
    """在任意嵌套结构中递归查找字段名"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def _num(x):
    try:
        return round(float(x), 4)
    except (TypeError, ValueError):
        return None


async def openrouter_endpoints(client: httpx.AsyncClient, model_id: str, api_key: str = None):
    """OpenRouter 专用（只读、免费、不耗额度）：查某模型的所有上游提供方及近期可用率。

    GET /api/v1/models/{id}/endpoints → 每个提供方的 status(0=正常)、uptime_last_5m/30m/1d、
    max_prompt_tokens、是否免费。返回 list[dict]；查询失败返回 None。
    :free 等变体 slug 若 404，会去掉冒号后缀重试一次。
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    for slug in (model_id, model_id.split(":")[0]):
        try:
            r = await client.get(f"https://openrouter.ai/api/v1/models/{slug}/endpoints",
                                 headers=headers, timeout=25)
        except Exception:
            return None
        if r.status_code == 404 and ":" in model_id and slug != model_id:
            continue
        if r.status_code != 200:
            return None
        eps = (r.json().get("data") or {}).get("endpoints") or []
        out = []
        for e in eps:
            p = e.get("pricing") or {}
            try:
                free = float(p.get("prompt") or 0) == 0 and float(p.get("completion") or 0) == 0
            except (TypeError, ValueError):
                free = False
            out.append({
                "provider": e.get("provider_name") or e.get("name") or "?",
                "status": e.get("status"),          # 0=正常，非 0=降级
                "free": free,
                "uptime_5m": e.get("uptime_last_5m"),
                "uptime_30m": e.get("uptime_last_30m"),
                "uptime_1d": e.get("uptime_last_1d"),
                "max_prompt_tokens": e.get("max_prompt_tokens"),
            })
        return out
    return None


def endpoints_note(eps) -> str:
    """把提供方状态压缩成一行可读摘要，供测试结果/错误信息附加"""
    if not eps:
        return ""
    parts = []
    for e in eps[:4]:
        up = e.get("uptime_1d")
        state = "正常" if e.get("status") == 0 else "降级"
        pct = f",1d可用率{up:.0f}%" if isinstance(up, (int, float)) else ""
        tag = "免费" if e.get("free") else "付费"
        parts.append(f"{e['provider']}({tag},{state}{pct})")
    more = f" 等{len(eps)}家" if len(eps) > 4 else ""
    return "上游: " + ", ".join(parts) + more
