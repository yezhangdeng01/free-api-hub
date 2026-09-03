"""各平台模型列表与额度查询"""
import httpx

# 智谱网页端额度接口（社区逆向，非官方文档，失败会自动降级）
ZHIPU_BALANCE_URL = "https://www.bigmodel.cn/api/biz/account/query-customer-account-report"


async def fetch_models(client: httpx.AsyncClient, base_url: str, api_key: str) -> list:
    """拉取渠道的模型列表（OpenAI 兼容 /models）"""
    r = await client.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else data
    ids = []
    for it in items:
        mid = it.get("id") if isinstance(it, dict) else str(it)
        if mid:
            ids.append(mid)
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
