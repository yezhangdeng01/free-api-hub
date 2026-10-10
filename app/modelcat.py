"""公开模型目录（models.dev）—— 自动补全「上游 /models 不列」的免费模型。

问题（2026-10-07 实测）：有些平台的模型**能调、`GET /models` 却不返回它**，
于是永远进不了渠道模型列表：

  · 智谱免费档：`glm-4-flash` / `glm-4.5-flash` / `glm-4.7-flash` / `glm-4.6v-flash`
    实测全部 200（账户余额 **¥0**），而智谱 `/models` 只返回 11 个主力/付费模型；
  · Cloudflare Workers AI：OpenAI 兼容端点**根本没有 `/models`**（实测 405）。

上一版让用户手填 `extra_models`，但「自己去查模型名」这件事本身就不该让人做。
这一版接一份公开目录自动给候选，再逐个**探活**确认后才并入（见 `gateway.auto_add_models`）。

数据源：https://models.dev/api.json —— 开源模型目录（226 个平台，全量 5 MB）。
只抽取「映射到的平台 + 标价 0」那部分，压缩成几 KB 缓存到
`data/model_catalog.json`，默认 24 小时更新一次（`model_catalog_hours`）。
拉不到（断网 / 代理不稳）就用内置 `SEED`，功能不中断。

⚠️ 三条不能改的判据：
  1. **判据是 `cost` 字段全 0，不是名字后缀**（同 `providers._is_zero_pricing` 的道理）；
  2. **没有 cost 字段的当付费**——宁缺勿滥；
  3. 目录数据比真实可用集**滞后是常态**（例：`glm-4.5-flash` 官方公告已下线、
     目录里仍标免费），所以候选**必须**过 `gateway._probe_candidates` 探活才算数。
"""
from __future__ import annotations

import io
import json
import logging
import os
import time

from . import store

logger = logging.getLogger("api-hub")

CATALOG_URL = "https://models.dev/api.json"

# api-hub 的平台类型 → models.dev 的 provider id。
# `custom` 有意不给：自建端点的模型命名无从猜，自动补全只会制造噪声。
PROVIDER_MAP = {
    "zhipu": "zhipuai",
    "zai": "zai",
    "openrouter": "openrouter",
    "gemini": "google",
    "openai": "openai",
    "nim": "nvidia",
    "siliconflow": "siliconflow-cn",
    "groq": "groq",
    "cerebras": "cerebras",
    "mistral": "mistral",
    "cohere": "cohere",
    "modelscope": "modelscope",
    "opencode": "opencode",
    "huggingface": "huggingface",
    "together": "togetherai",
    "agnes": "agnes",
}

# 离线种子：目录拉不到时（断网 / 代理不稳 / models.dev 改版）仍要有候选。
# 只放**实测确认免费且能调**的，宁缺勿滥 —— 探活还会再筛一遍。
SEED = {
    "zhipu": ["glm-4-flash", "glm-4.5-flash", "glm-4.7-flash", "glm-4.6v-flash"],
    "zai": ["glm-4.5-flash", "glm-4.7-flash", "glm-4.6v-flash"],
}

_catalog: dict | None = None    # {平台类型: [模型 id, ...]}
_ts: float = 0.0                # 上次成功抓到目录的时间


def _extract(raw: dict) -> dict:
    """从 models.dev 全量数据里抽出「映射到的平台上、标价 0」的模型 id。

    返回 {平台类型: [模型 id, ...]}。没映射的平台直接不看 —— 我们只填自己
    能建出来的渠道，多存的每一个字都是白占磁盘。
    """
    out: dict = {}
    for ch_type, pid in PROVIDER_MAP.items():
        prov = raw.get(pid) or {}
        models = prov.get("models") or {}
        ids = []
        for mid, m in models.items():
            if not isinstance(m, dict):
                continue
            cost = m.get("cost")
            if not isinstance(cost, dict):
                continue        # 没标价 → 当付费（宁缺勿滥）
            # 必须**两个方向都明确标 0**。少一个字段（None）或只标了 input 都算数据不全，
            # 一律当付费 —— 这条放宽的代价是用户真的会被扣钱，宁可漏掉一个免费模型。
            if cost.get("input") != 0 or cost.get("output") != 0:
                continue
            name = str(m.get("id") or mid).strip()
            if name:
                ids.append(name)
        if ids:
            out[ch_type] = sorted(set(ids))
    return out


def save_cache():
    try:
        os.makedirs(os.path.dirname(store.MODEL_CATALOG_PATH), exist_ok=True)
        with io.open(store.MODEL_CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": _ts, "catalog": _catalog or {}}, f,
                      ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning("模型目录缓存写入失败（不影响运行）: %s", e)


def load_cache() -> dict:
    """恢复磁盘上的目录缓存。**内存已有就不重读**（跟 freemodels 一样）。"""
    global _catalog, _ts
    if _catalog is not None:
        return _catalog
    try:
        with io.open(store.MODEL_CATALOG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        _catalog = d.get("catalog") or {}
        _ts = float(d.get("ts") or 0)
    except Exception:
        _catalog = {}
        _ts = 0.0
    return _catalog


def update_from_raw(raw: dict, ts: float | None = None) -> dict:
    global _catalog, _ts
    _catalog = _extract(raw)
    _ts = time.time() if ts is None else ts
    save_cache()
    return _catalog


def cache_age_hours():
    """没抓过返回 None（调用方按「过期」处理）。"""
    if not _ts:
        return None
    return (time.time() - _ts) / 3600


async def refresh(client) -> dict:
    """拉一次 models.dev 全量目录并抽取。异常原样抛出，由调用方兜底。"""
    r = await client.get(CATALOG_URL, timeout=60)
    r.raise_for_status()
    cat = update_from_raw(r.json())
    n = sum(len(v) for v in cat.values())
    logger.info("公开模型目录已更新：%d 个平台 / %d 个标价 0 的模型", len(cat), n)
    return cat


def catalog_for(ch_type: str) -> list:
    """某平台的免费候选 = 在线目录 ∪ 内置种子（顺序稳定）。"""
    got = list((load_cache() or {}).get(ch_type) or [])
    for m in SEED.get(ch_type) or []:
        if m not in got:
            got.append(m)
    return got


def candidates_for(ch_type: str, upstream_ids=(), exclude=()) -> list:
    """该平台「上游没列、也没被手填」的免费候选（纯函数，便于测）。"""
    if not ch_type:
        return []
    have = set(upstream_ids or ()) | set(exclude or ())
    return [m for m in catalog_for(ch_type) if m not in have]
