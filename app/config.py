"""配置读写与渠道预设（敏感字段经 DPAPI 加密落盘）"""
import json
import logging
import os
import sys
import threading
import uuid

from . import vault

logger = logging.getLogger("api-hub")

if getattr(sys, "frozen", False):
    # PyInstaller 打包（绿色版）：数据文件（config.json/data/frontend）都在 exe 同目录
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

# 各平台预设：base_url 均为 OpenAI 兼容地址
PROVIDER_PRESETS = {
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
    },
    "nim": {
        "label": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "custom": {
        "label": "自定义 (OpenAI 兼容)",
        "base_url": "",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "cerebras": {
        "label": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
    },
    "cohere": {
        "label": "Cohere",
        "base_url": "https://api.cohere.ai/compatibility/v1",
    },
    "modelscope": {
        "label": "魔搭 ModelScope",
        "base_url": "https://api-inference.modelscope.cn/v1",
    },
    "opencode": {
        "label": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
    },
    "huggingface": {
        "label": "HuggingFace Router",
        "base_url": "https://router.huggingface.co/v1",
    },
    "together": {
        "label": "Together AI",
        "base_url": "https://api.together.xyz/v1",
    },
    "zai": {
        "label": "智谱国际 Z.ai",
        "base_url": "https://api.z.ai/api/paas/v4",
    },
    "agnes": {
        "label": "Agnes AI",
        "base_url": "https://apihub.agnes-ai.com/v1",
    },
}

DEFAULT_CONFIG = {
    "port": 8787,
    # 健康检查只拉 /models 列表（元数据，零 token），30 分钟一次足够及时
    "check_interval_minutes": 30,
    # 额度查询无生成成本，15 分钟一次
    "quota_interval_minutes": 15,
    # 模型 1-token 实测探测：会消耗免费额度 / 占请求数，60 分钟一次，
    # 避免把 OpenRouter :free 这类按天计次的小限额烧在探测上
    "probe_interval_minutes": 60,
    # AA 榜分/视觉能力的缓存新鲜度阈值（小时）：超过才去 OpenRouter 公开端点补一次。
    # 榜分是月级更新的，没必要每次刷新都拿；本地缓存永不失效，只是「旧了就去补」。
    "bench_cache_hours": 24,
    "auth_enabled": True,
    "api_token": "",
    "aliases": {},
    "pinned": [],
    # 视觉专属收藏（界面「视觉」视图里的 ★，2026-09-20 用户要求与主收藏分开）：
    # 只影响「视觉」视图的排序与 `auto-vision` 的实际切换顺序，不参与其它策略的候选排序。
    "pinned_vision": [],
    "channels": [],
    "route_strategy": "balanced",    # balanced / quality / stability / speed
    "probe_used_models": True,       # 是否启用模型 1-token 主动探测
    "adaptive_preemption": True,     # 429 自学水位：预计要越线时提前换路
    # 手动档位（界面点档位 chip 写的，{模型id: 1|2|3}）。与 model_tiers 的分工：
    # 这里是「点名」——按模型 id 原文精确匹配；model_tiers 是「批量规则」——正则匹配。
    # 优先级：model_tier_exact > model_tiers > AA 榜分 > 名字启发式（见 capability.tier_overrides）。
    "model_tier_exact": {},
    "model_tiers": None,             # 手改的正则覆盖，默认不写（null = 没有规则）
    # ---- /v1/responses 翻译层（见 app/responses.py）----
    # 流式请求时给上游加 stream_options.include_usage，好让 response.completed 里有
    # token 用量。主流兼容层都认；哪个渠道因此报 400 就关掉（关掉只丢用量，不影响功能）。
    "responses_stream_usage": True,
    # 把 Responses 的 reasoning.effort 译成 chat 的 reasoning_effort 传给上游。
    # 默认关：个别渠道不认这个字段会整条 400，而少了它只是丢掉一个提示。
    "responses_reasoning_effort": False,
}

_lock = threading.Lock()


def _map_secrets(cfg: dict, fn):
    """对配置里的敏感字段（渠道 Key、网关 Token）应用加/解密函数"""
    tok = cfg.get("api_token")
    if isinstance(tok, str) and tok:
        cfg["api_token"] = fn(tok)
    for ch in cfg.get("channels", []):
        key = ch.get("api_key")
        if isinstance(key, str) and key:
            ch["api_key"] = fn(key)


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        save_config(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))
    # 磁盘上的密文解密为内存明文
    _map_secrets(cfg, vault.decrypt)
    # 首次启用鉴权时自动生成网关 Token
    if cfg.get("auth_enabled", True) and not cfg.get("api_token"):
        import secrets
        cfg["api_token"] = "ah_" + secrets.token_hex(16)
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    with _lock:
        import copy
        disk = copy.deepcopy(cfg)
        _map_secrets(disk, vault.encrypt)  # 落盘前加密敏感字段
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(disk, f, ensure_ascii=False, indent=2)


def new_channel_id() -> str:
    return "ch_" + uuid.uuid4().hex[:8]
