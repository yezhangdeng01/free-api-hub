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
# 这些家族的 flash 是同代主力（Gemini-3-flash 常是免费首选、glm-5.3-flash 与 glm-5.3
# 同代同档），所以同代的 flash 不降档；其他家族的 flash 只降到中档（不是轻量）。
# deepseek 于 2026-09 加入：实测 v4-flash-0731 的 AA 智能指数 34.5 > v4-pro 的 30.9/36.3，
# 且它是用户日常主力，不能按「加速档」降到中档。
_FLASH_OK_FAMILIES = {"gemini", "glm", "deepseek"}
# 同代里「够不够新」：只认同一代内**最新那个次版本**（0.05 容差），更早的次版本不给智能档
# ——同代旗舰之间能力差异也很大（实测 glm-5.1 的 AA 26.4 vs glm-5.3 的 44.9、
#   gemini-3.5-flash 33.0 vs gemini-3.8-flash 41.2）。有榜单分的模型以榜分为准，不受此限。
_FRONTIER_BAND = 0.05
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
# 无名家族的「旗舰像」标记：命中就给中档（否则一律轻量——不认识就保守压低）
_FLAGSHIP = re.compile(
    r"(?:^|[^a-z0-9])(?:ultra|super|max|pro|large|big|xlarge|xl|2\.4t)(?:$|[^a-z0-9])", re.I)


# ---- 权威榜单分：Artificial Analysis 智能指数（经 OpenRouter 白拿，不必额外申请密钥）----
# 渠道健康检查本来就在调 <base>/models，OpenRouter 的返回里带
# `benchmarks.artificial_analysis.intelligence_index`（439 个模型里约 90 个有分）。
# providers.fetch_models() 顺带把它喂进来：**有榜分就用榜分定档，没有才用下面的命名启发式**。
# 阈值取观测分布的分位数（p72 / p40），这样 AA 换算法/换版本（v4.2 → v5）时会自动适应。
_bench: dict = {}
_bench_hi: float = 33.0    # 智能档阈值
_bench_mid: float = 17.0   # 中档阈值
_bench_p10: float = 7.8    # 观测分布 p10（把榜分归一化到 0~1 用）
_bench_p90: float = 42.3   # 观测分布 p90
_BENCH_MIN_N = 20          # 榜分样本不足时沿用上面的默认阈值
# 无榜分时按档位给的「能力分锚点」：取该档位在归一化尺度上的**下沿**（保守，
# 不让没上榜的模型凭档位挤到榜上有名的模型前面）
_TIER_ANCHOR = {3: 0.76, 2: 0.28, 1: 0.10}
# 用户在 model_tiers 里显式指定的档位 → 给该档**顶值**（他们说了算，不再保守压低）
_OVERRIDE_ANCHOR = {3: 1.0, 2: 0.6, 1: 0.2}


def override_tier(model_id: str, overrides: dict = None):
    """config.json `model_tiers` 的显式指定（正则 → 档位）；没命中返回 None"""
    for pat, t in (overrides or {}).items():
        try:
            if re.search(pat, model_id or "", re.I) and int(t) in (1, 2, 3):
                return int(t)
        except (re.error, ValueError, TypeError):
            continue
    return None


def capability_score(model_id: str, tier: int = None, overrides: dict = None) -> float:
    """0~1 能力分（连续）。优先级：用户显式覆盖 > AA 榜分归一化 > 档位锚点。

    榜分按观测分布归一化（p10→0、p90→1）；无榜分时用档位锚点（保守）。
    「智能优先」用这个连续分排序——只按 3 档太钝（同档里 AA 41.2 与 33.9 差 7 分）。"""
    ov = override_tier(model_id, overrides)
    if ov is not None:
        return _OVERRIDE_ANCHOR.get(ov, 0.6)
    s = _bench.get(norm_id(model_id))
    if s is not None and _bench_p90 > _bench_p10:
        return max(0.0, min(1.0, (s - _bench_p10) / (_bench_p90 - _bench_p10)))
    t = tier if tier is not None else tier_of(model_id)
    return _TIER_ANCHOR.get(t, _TIER_ANCHOR[2])


def norm_id(model_id: str) -> str:
    """跨渠道对齐用的归一化模型名：去厂商前缀与 :free/:batch 等变体后缀，转小写。

    `ZhipuAI/GLM-5.3-Flash` / `z-ai/glm-5.3-flash:batch` / `models/gemini-3.8-flash`
    都能落到同一个 key。"""
    s = (model_id or "").lower().split("/")[-1]
    for suf in (":free", ":batch", ":nitro", ":extended", ":online", ":thinking"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


def update_bench_scores(scores: dict) -> dict:
    """合并榜分（{归一化名: 智能指数}）并重算分档阈值，返回 {n, hi, mid} 便于日志"""
    global _bench_hi, _bench_mid, _bench_p10, _bench_p90
    clean = {k: float(v) for k, v in (scores or {}).items() if isinstance(v, (int, float))}
    if clean:
        _bench.update(clean)
        vals = sorted(_bench.values())
        if len(vals) >= _BENCH_MIN_N:
            _bench_hi = round(vals[min(len(vals) - 1, int(len(vals) * 0.72))], 1)
            _bench_mid = round(vals[min(len(vals) - 1, int(len(vals) * 0.40))], 1)
            _bench_p10 = vals[min(len(vals) - 1, int(len(vals) * 0.10))]
            _bench_p90 = vals[min(len(vals) - 1, int(len(vals) * 0.90))]
    return {"n": len(_bench), "hi": _bench_hi, "mid": _bench_mid}


def bench_of(model_id: str):
    """该模型的 Artificial Analysis 智能指数（没上榜 → None）"""
    return _bench.get(norm_id(model_id))


# ---- 视觉能力：同样从渠道 /models 白拿（OpenRouter 的 architecture.input_modalities）----
# 名字启发式（下面的 _VISION）会漏掉大量真·多模态模型（Qwen3.5/3.6/3.8 全系、gemma-3/4、
# GLM-5.3-Flash、MiniMax-M3、claude-fable-5、nova、kimi-k2.5…实测漏了 250+ 个），
# 也会把 TTS/音频这类误标成视觉。所以：**有榜单/平台数据就用数据，没有才回退名字启发式**。
_vision_ok: set = set()   # 归一化名：确认支持图像输入
_vision_no: set = set()   # 归一化名：确认**不**支持（用于纠正启发式的误标，如 TTS）


def update_vision_models(ok, no) -> dict:
    """合并「支持/不支持图像输入」的模型名（归一化），返回计数便于日志"""
    _vision_ok.update(ok or ())
    _vision_no.update(no or ())
    return {"ok": len(_vision_ok), "no": len(_vision_no)}


def vision_known(model_id: str):
    """True/False=平台数据明确；None=没数据（回退名字启发式）"""
    nid = norm_id(model_id)
    if nid in _vision_ok:
        return True
    if nid in _vision_no:
        return False
    return None


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
    ov = override_tier(model_id, overrides)
    if ov is not None:
        return ov
    # 1) 非对话模型（图片/视频/音频/向量/翻译/安全）：不能拿来聊天就别占智能档
    if _UTIL.search(model_id):
        return 1
    # 2) 权威榜单分：有 Artificial Analysis 智能指数就用它定档（比认名字可靠得多）
    score = _bench.get(norm_id(model_id))
    if score is not None:
        return 3 if score >= _bench_hi else (2 if score >= _bench_mid else 1)
    # 3) 轻量：小尺寸 SKU / 名字写明 ≤9B（规模决定，与代际无关）
    if _SMALL_SKU.search(model_id):
        return 1
    pb = _params_b(model_id)
    if pb is not None and pb <= _TINY_PARAMS:
        return 1
    huge = pb is not None and pb >= _HUGE_PARAMS

    # 4) 已知家族：代际（主版本号）+ 次版本前沿带 + 档位标记 逐级降档
    fam, ver = _family_version(model_id)
    if fam:
        cur = effective_min(fam)
        same_gen = int(ver) >= int(cur)      # 主版本号相同即同代（3.6 与 3.8 同属第 3 代）
        fresh = ver >= cur - _FRONTIER_BAND  # 同代里也要够新，早期次版本不给智能档
        fast, lite = bool(_FAST.search(model_id)), bool(_LITE.search(model_id))
        if same_gen and fresh:
            # 同代：旗舰 3 → 加速档(flash/turbo) 2 → 缩水档(lite/air) 1
            # 例外：gemini/glm/deepseek 的 flash 就是同代主力（免费首选），不降档
            if lite:
                tier = 1
            elif fast and fam not in _FLASH_OK_FAMILIES:
                tier = 2
            else:
                tier = 3
        else:
            # 落后代 / 同代早期版本：整体降一档（旗舰→中档，不塌到轻量），带标记再降一档
            tier = 1 if (fast or lite) else 2
        tier = max(1, tier)
        # 名字里写明了中等规模（10~200B）就封顶中档：同代 ≠ 同尺寸
        # （Qwen3-14B、DeepSeek-R1-Distill-Qwen-14B 这类）
        if pb is not None and _TINY_PARAMS < pb < _HUGE_PARAMS:
            tier = min(tier, 2)
        return tier

    # 5) 无名家族：默认轻量（不认识就保守压低），只有「旗舰像」才给中档
    if huge or any(re.search(p, model_id, re.I) for p in _STRONG) or _FLAGSHIP.search(model_id):
        return 2
    return 1


# ---------------- 视觉/上下文启发式（模型详情用，非官方数据，仅推断） ----------------
# 注意：**只在 `vision_known()` 没数据时才用它**（平台数据优先，见上）
_VISION = [
    r"vision", r"-?vlx?(\b|-|$)", r"\.vl(\b|-|$)", r"omni", r"multimodal",
    r"gpt-4o(?!.*mini)", r"glm-4v", r"glm-5v", r"gemini-.*", r"qwen.*-?vl",
    r"llama.*vision", r"claude-3", r"grok-2?-?vision", r"moonshot.*-vl",
]
# 名字里明确**不是**图像输入的（纯生成/语音类）：防止启发式把它们标成「视觉」
_VISION_NEG = re.compile(
    r"(?:^|[-_ .])(?:tts|lyria|music|audio|speech|whisper|dall|flux|sdxl|diffusion)"
    r"(?:$|[-_ .])|image(?:-preview)?$", re.I)
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
    """[是否视觉 / 上下文档位]。视觉优先用平台数据（vision_known），没数据才用名字启发式。

    上下文仍然只能靠名字推断（渠道 /models 各平台返回的字段不统一），仅供参考。"""
    known = vision_known(model_id) if model_id else None
    if known is None:
        known = bool(model_id) and not _VISION_NEG.search(model_id) \
            and any(re.search(p, model_id, re.I) for p in _VISION)
    ctx = None
    if model_id:
        for pat, label in _CONTEXT_HINTS:
            if re.search(pat, model_id, re.I):
                ctx = label
                break
    return {"vision": known, "context": ctx}
