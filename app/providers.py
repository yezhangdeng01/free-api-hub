"""各平台模型列表与额度查询"""
import logging

import httpx

from app import capability

logger = logging.getLogger("api-hub")

# 智谱网页端额度接口（社区逆向，非官方文档，失败会自动降级）
ZHIPU_BALANCE_URL = "https://www.bigmodel.cn/api/biz/account/query-customer-account-report"


async def fetch_models(client: httpx.AsyncClient, base_url: str, api_key: str) -> list:
    """拉取渠道的模型列表（OpenAI 兼容 /models）

    顺带收割权威榜单分：OpenRouter 的返回里带
    `benchmarks.artificial_analysis.intelligence_index`（Artificial Analysis 智能指数），
    这是白拿的——不用额外申请密钥、不多发一次请求，喂给 capability 用它定能力档位。
    """
    r = await client.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else data
    ids, bench = [], {}
    for it in items:
        if not isinstance(it, dict):
            if it:
                ids.append(str(it))
            continue
        mid = it.get("id")
        if not mid:
            continue
        ids.append(mid)
        aa = ((it.get("benchmarks") or {}).get("artificial_analysis") or {})
        v = aa.get("intelligence_index")
        if isinstance(v, (int, float)):
            bench[capability.norm_id(mid)] = v
    if bench:
        info = capability.update_bench_scores(bench)
        logger.info("收割 AA 榜分 %d 个（累计 %d，智能阈值 %.1f / 中档阈值 %.1f）",
                    len(bench), info["n"], info["hi"], info["mid"])
    return sorted(set(ids))


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
