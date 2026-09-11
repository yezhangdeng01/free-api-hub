"""模型能力分层启发式

按模型名关键词估算能力档位（1=轻量 / 2=中档 / 3=智能），用于「智能优先」路由策略。
这是启发式分层，不是跑分数据；用户可在 config.json 的 model_tiers 里用正则覆盖：
    "model_tiers": {"qwen.*max": 3, ".*-flash": 1}

三条轴，先命中先返回（顺序即优先级）：
  1) **规模**决定「轻量」：小尺寸 SKU（mini/nano/tiny/haiku/micro/small）、
     ≤9B 参数、非对话模型（guard/embed/rerank/translate/image/video/audio…）。
     轻量 = 「小或不是干这个的」，与代际无关。
  2) **代际**只降一档：家族当前代非加速档 = 智能(3)；当前代加速档 / 落后一代 = 中档(2)。
     不会因为「落后一代」就掉到轻量——那是旧版的问题
     （gemini-3.5-flash、deepseek-v4-flash 曾被压成轻量，与 8B 小模型同档）。
  3) **代」以主版本号计**（3.6 与 3.8 同属第 3 代），避免厂商小版本号把上一代打成旧货。
"""
import re

# 非对话主力：不能拿来聊天的就别占智能档（图片/视频/音频/向量/翻译/安全）
_UTIL = re.compile(
    r"(?:^|[^a-z0-9])(?:guard|safety|moderation|embed|embedding|rerank|translate|transcribe|ocr"
    r"|whisper|asr|tts|speech|voice|image|video|audio|music|lyria|dall|flux|sdxl|diffusion)"
    r"(?:$|[^a-z0-9])", re.I)
# 小尺寸 SKU（独立词，防误伤 gemini 里的 mini、minimax 等）
_SMALL_SKU = re.compile(
    r"(?:^|[^a-z0-9])(?:mini|nano|tiny|haiku|micro|small)(?:$|[^a-z0-9])", re.I)
# 加速档：同代里更快更便宜的那一档（不是小模型）
_FAST = re.compile(r"(?:^|[^a-z0-9])(?:flash|turbo|instant|fast|lightning)(?:$|[^a-z0-9])", re.I)
# 缩水档：名字自带「砍过」的语义，同代也给轻量
_LITE = re.compile(r"(?:^|[^a-z0-9])(?:lite|air|light)(?:$|[^a-z0-9])", re.I)
# 参数量（十亿）：`120b` / `2.4T`；`a12b`(激活参数) 因前面是字母不会被匹配
_PARAM = re.compile(r"(?:^|[^a-z0-9])(\d+(?:\.\d+)?)([bt])(?![a-z0-9])", re.I)
# 不用认家族、光看名字就够强的信号（超大规模旗舰）
_STRONG = [r"(?:^|[^a-z0-9])ultra(?:$|[^a-z0-9])"]

# ---- 家族代际门槛：每个家族只有「≥ 当前代」才算智能档 ----
# 版本号即“代际刻度”：改这一个数字就能跟上厂商发版节奏，不用重写模型名单。
#   例：某天出了 claude-6 → 把 "claude" 的最小值改成 6.0，老代自动全部降档。
_FAMILY_MIN_VERSION = {
    "gpt": 5.0, "o": 4.0, "qwen": 3.8, "gemini": 3.0,
    "claude": 4.0, "grok": 4.0, "deepseek": 4.0,
    "kimi": 2.0, "glm": 5.0, "minimax": 3.0,
}
# 这些家族的 flash 就是同代主力（Gemini-3-flash 常是免费首选、glm-5.3-flash 与 glm-5.3
# 同代同档），所以同代的 flash 不降档；其他家族的 flash 只降到中档（不是轻量）
_FLASH_OK_FAMILIES = {"gemini", "glm"}
# 各家解析方式：返回 (家族, 版本号浮点)
# 注意分隔符要跟厂商命名一致：`qwen3.8` 无连字符、`gemini-3.5` 有 —— 写宽了会把
# 参数量当版本号（如 `DeepSeek-R1-Distill-Qwen-14B` → 以为是 qwen 第 14 代）。
_FAMILY_PARSERS = [
    ("gpt",     re.compile(r"gpt-(\d+)", re.I)),
    ("o",       re.compile(r"\bo(\d+)(?:-|\b)", re.I)),
    ("qwen",    re.compile(r"qwen(\d+(?:\.\d+)?)", re.I)),
    ("gemini",  re.compile(r"gemini-(\d+(?:\.\d+)?)", re.I)),
    ("claude",  re.compile(r"claude-(\d+)(?:[-.](\d+))?", re.I)),
    ("grok",    re.compile(r"grok-(\d+)", re.I)),
    ("deepseek", re.compile(r"deepseek-v(\d+)", re.I)),
    ("kimi",    re.compile(r"kimi-k(\d+)", re.I)),
    ("glm",     re.compile(r"glm-(\d+)(?:\.(\d+))?", re.I)),
    ("minimax", re.compile(r"minimax.*[mM](\d+(?:\.\d+)?)", re.I)),
]
# 参数规模门槛（十亿）
_TINY_PARAMS = 9.0      # ≤9B → 轻量
_HUGE_PARAMS = 200.0    # ≥200B → 至少中档，且可判智能


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


def _params_b(model_id: str):
    """粗解析总参数量（十亿）；取名字里最大的 b/T 标记，没有则 None。

    `nemotron-3-super-120b-a12b` → 120（a12b 是激活参数，前面是字母，不匹配）
    `Qwen3.8-2.4T-A95B` → 2400
    """
    best = None
    for m in _PARAM.finditer(model_id or ""):
        val = float(m.group(1)) * (1000.0 if m.group(2).lower() == "t" else 1.0)
        if best is None or val > best:
            best = val
    return best


# ---- 自适应前沿：程序自己「看到」的当前最高代 ----
# 每次渠道健康检查拉回模型清单后调用 update_frontier()，
# 之后家族门槛取 max(静态默认值, 观测到的最高代)。
# 效果：某天任意渠道出现 gpt-6，gpt-5 会**自动**降为中档，
# 无需等任何人改表——这就是开源后能长期自维护的机制。
_observed_frontier: dict = {}


def update_frontier(model_ids) -> dict:
    """从当前所有渠道可见的模型清单里，统计每个家族观测到的最高代。

    忽略小尺寸 SKU（mini/nano/tiny/haiku/micro/small）——它们不代表该家族旗舰代际。
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
    """返回能力档位 1/2/3，overrides 为 {正则: 档位}

    档位含义：3=智能（同代旗舰/超大规模）｜2=中档（同代加速档、落后代、无名但非小模型）
    ｜1=轻量（小尺寸/≤9B/非对话模型）。判不了就给 2——宁保守不冒进。
    """
    if not model_id:
        return 2
    for pat, t in (overrides or {}).items():
        try:
            if re.search(pat, model_id, re.I) and int(t) in (1, 2, 3):
                return int(t)
        except (re.error, ValueError, TypeError):
            continue
    # 1) 轻量：非对话模型 / 小尺寸 SKU / ≤9B —— 只由「规模」决定，与代际无关
    if _UTIL.search(model_id):
        return 1
    if _SMALL_SKU.search(model_id):
        return 1
    pb = _params_b(model_id)
    if pb is not None and pb <= _TINY_PARAMS:
        return 1
    huge = pb is not None and pb >= _HUGE_PARAMS

    # 2) 已知家族：按代际（主版本号）+ 档位标记 逐级降档
    fam, ver = _family_version(model_id)
    if fam:
        cur = effective_min(fam)
        same_gen = int(ver) >= int(cur)      # 主版本号相同即同代（3.6 与 3.8 同属第 3 代）
        fast, lite = bool(_FAST.search(model_id)), bool(_LITE.search(model_id))
        if same_gen:
            # 同代：旗舰 3 → 加速档(flash/turbo) 2 → 缩水档(lite/air) 1
            # 例外：gemini/glm 的 flash 就是同代主力（免费首选），不降档
            if lite:
                tier = 1
            elif fast and fam not in _FLASH_OK_FAMILIES:
                tier = 2
            else:
                tier = 3
        else:
            # 落后代：整体降一档（旗舰→中档，不塌到轻量），带档位标记再降一档
            tier = 1 if (fast or lite) else 2
        tier = max(1, tier)
        # 名字里写明了中等规模（10~200B）就封顶中档：同代 ≠ 同尺寸
        # （Qwen3-14B、DeepSeek-R1-Distill-Qwen-14B 这类）
        if pb is not None and _TINY_PARAMS < pb < _HUGE_PARAMS:
            tier = min(tier, 2)
        return tier

    # 3) 无名家族：只信「规模」
    if huge or any(re.search(p, model_id, re.I) for p in _STRONG):
        return 3
    # 没有代际可比时，flash/turbo 只当「同代快速档」→ 中档（不是小模型）；
    # 只有 lite/air 这种名字自带「砍过」语义的才给轻量
    return 1 if _LITE.search(model_id) else 2


# ---------------- 视觉/上下文启发式（模型详情用，非官方数据，仅推断） ----------------
_VISION = [
    r"vision", r"-?vlx?(\b|-|$)", r"\.vl(\b|-|$)", r"omni", r"multimodal",
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
