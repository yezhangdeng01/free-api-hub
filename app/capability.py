"""模型能力分层启发式

按模型名关键词估算能力档位（1=轻量 / 2=中档 / 3=强），用于"智能优先"路由策略。
这是启发式分层，不是跑分数据；用户可在 config.json 的 model_tiers 里用正则覆盖：
    "model_tiers": {"qwen.*max": 3, ".*-flash": 1}
"""
import re

# 不按「家族代际」管理、只靠命名的强档信号（通常是超大规模模型）
_STRONG = [
    r"nemotron.*(super|ultra)",
]
# 轻量后缀必须是独立词（防误伤 gemini 里的 mini、minimax 等）
_WORD = r"(?:^|[^a-z0-9])"
_WEAK = [
    r"(?:^|[^a-z0-9])(?:mini|nano|tiny|haiku|flash|turbo|lite|air|guard)(?:$|[^a-z0-9])",
    r"[-:](1|2|3|4|7|8)b(\b|-|\.|$)", r"1\.5b", r"4b",
]

# ---- 家族代际门槛：每个家族只有「≥ 当前代」才算智能档 ----
# 版本号即“代际刻度”：改这一个数字就能跟上厂商发版节奏，不用重写模型名单。
#   例：某天出了 claude-6 → 把 "claude" 的最小值改成 6.0，老代自动全部降档。
_FAMILY_MIN_VERSION = {
    "gpt": 5.0, "o": 4.0, "qwen": 3.8, "gemini": 3.0,
    "claude": 4.0, "grok": 4.0, "deepseek": 4.0,
    "kimi": 2.0, "glm": 5.0, "minimax": 3.0,
}
# 这些家族里的 flash/turbo/lite 变体也可能是旗舰（如 Gemini-3-flash 常是免费主力、
# glm-5.3-flash 与 glm-5.3 同代），所以当前代不强行走弱档
_FLASH_OK_FAMILIES = {"gemini", "glm"}
# 各家解析方式：返回 (家族, 版本号浮点)
_FAMILY_PARSERS = [
    ("gpt",     re.compile(r"gpt-(\d+)", re.I)),
    ("o",       re.compile(r"\bo(\d+)(?:-|\b)", re.I)),
    ("qwen",    re.compile(r"qwen(\d+(?:\.\d+)?)", re.I)),
    ("gemini",  re.compile(r"gemini-(\d+(?:\.\d+)?)", re.I)),
    ("claude",  re.compile(r"claude-(\d+)(?:-(\d+))?", re.I)),
    ("grok",    re.compile(r"grok-(\d+)", re.I)),
    ("deepseek", re.compile(r"deepseek-v(\d+)", re.I)),
    ("kimi",    re.compile(r"kimi-k(\d+)", re.I)),
    ("glm",     re.compile(r"glm-(\d+)(?:\.(\d+))?", re.I)),
    ("minimax", re.compile(r"minimax.*[mM](\d+(?:\.\d+)?)", re.I)),
]
_SMALL_SKU = re.compile(r"(?:^|[^a-z0-9])(?:mini|nano|tiny|haiku)(?:$|[^a-z0-9])", re.I)


def _family_version(model_id: str):
    """识别 (家族, 版本号)；Claude 的 3-5 / 4-6 折成 3.5 / 4.6"""
    for fam, rx in _FAMILY_PARSERS:
        m = rx.search(model_id or "")
        if not m:
            continue
        if fam == "claude":
            major = int(m.group(1))
            ver = float(m.group(2)) / 10 + major if m.group(2) else float(major)
            return fam, ver
        return fam, float(m.group(1))
    return None, None


# ---- 自适应前沿：程序自己「看到」的当前最高代 ----
# 每次渠道健康检查拉回模型清单后调用 update_frontier()，
# 之后家族门槛取 max(静态默认值, 观测到的最高代)。
# 效果：某天任意渠道出现 gpt-6，gpt-5 会**自动**降为中档，
# 无需等任何人改表——这就是开源后能长期自维护的机制。
_observed_frontier: dict = {}


def update_frontier(model_ids) -> dict:
    """从当前所有渠道可见的模型清单里，统计每个家族观测到的最高代。

    忽略小尺寸 SKU（mini/nano/tiny/haiku）——它们不代表该家族旗舰代际。
    返回并缓存 {家族: 最高版本}。
    """
    frontier: dict = {}
    for mid in model_ids:
        if _SMALL_SKU.search(mid or ""):
            continue
        fam, ver = _family_version(mid)
        if fam and ver > frontier.get(fam, 0):
            frontier[fam] = ver
    _observed_frontier.update(frontier)
    return dict(frontier)


def effective_min(fam: str) -> float:
    """该家族生效的智能档门槛 = max(静态默认, 观测前沿)"""
    return max(_FAMILY_MIN_VERSION.get(fam, 0),
               _observed_frontier.get(fam, 0))


def tier_of(model_id: str, overrides: dict = None) -> int:
    """返回能力档位 1/2/3，overrides 为 {正则: 档位}"""
    if not model_id:
        return 2
    for pat, t in (overrides or {}).items():
        try:
            if re.search(pat, model_id, re.I) and int(t) in (1, 2, 3):
                return int(t)
        except (re.error, ValueError, TypeError):
            continue
    fam, ver = _family_version(model_id)
    if fam and ver >= effective_min(fam):
        # 当前代：小尺寸 SKU（mini/nano/tiny/haiku）归轻量；
        # flash/turbo/lite/air 只对特殊家族放行，其余压到轻量
        if _SMALL_SKU.search(model_id):
            return 1
        flashish = re.search(r"flash|turbo|lite|air", model_id, re.I)
        if flashish and fam not in _FLASH_OK_FAMILIES:
            return 1
        return 3
    # 其余情况（无家族 / 上一代）：回到通用规则
    if any(re.search(p, model_id, re.I) for p in _STRONG):
        return 3
    if any(re.search(p, model_id, re.I) for p in _WEAK):
        return 1
    return 2


# ---------------- 视觉/上下文启发式（模型详情用，非官方数据，仅推断） ----------------
_VISION = [
    r"vision", r"-?vlx?(\b|-)", r"\.vl(\b|-)", r"omni", r"multimodal",
    r"gpt-4o(?!.*mini)", r"glm-4v", r"glm-5v", r"gemini-.*", r"qwen.*-?vl",
    r"llama.*vision", r"claude-3", r"grok-2?-?vision", r"moonshot.*-vl",
]
_CONTEXT_HINTS = [
    (r"2m|2,?000,?000|2097152", "2M"),
    (r"1m|1,?000,?000|1048576", "1M"),
    (r"200k|200,?000|200000", "200K"),
    (r"131072|128k", "128K"),
    (r"100,?000|100000|100k", "100K"),
    (r"65536|64k", "64K"),
    (r"32768|32k", "32K"),
]


def meta_of(model_id: str) -> dict:
    """按模型名推断 [是否视觉 / 上下文档位]。推断仅供参考，勿当官方参数使用"""
    vision = bool(model_id) and any(re.search(p, model_id, re.I) for p in _VISION)
    ctx = None
    if model_id:
        for pat, label in _CONTEXT_HINTS:
            if re.search(pat, model_id, re.I):
                ctx = label
                break
    return {"vision": vision, "context": ctx}
