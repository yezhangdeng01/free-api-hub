"""公开模型目录（modelcat）：抽取判据、平台映射、候选计算。

重点在**判据**上：`cost` 全 0 才算免费。这几条如果松了，会把付费模型当免费
自动加进用户渠道（用户会真的为此付钱），所以每条都配了反向用例。
"""
import json
import os

from app import modelcat, store


# ---------- 抽取：什么算「免费」 ----------

def test_extract_keeps_zero_cost_only():
    raw = {"zhipuai": {"models": {
        "glm-4.7-flash": {"cost": {"input": 0, "output": 0, "cache_read": 0}},
        "glm-4.6": {"cost": {"input": 0.6, "output": 2.2}},
    }}}
    assert modelcat._extract(raw) == {"zhipu": ["glm-4.7-flash"]}


def test_extract_treats_missing_cost_as_paid():
    """没有 cost 字段 → 当付费。宁缺勿滥：目录里不少模型压根不标价，
    把「没标价」当免费会往用户渠道里灌一堆要花钱的模型。"""
    raw = {"zhipuai": {"models": {"glm-mystery": {}}}}
    assert modelcat._extract(raw) == {}


def test_extract_requires_both_directions_free():
    """输入免费、输出收费 → **不算免费**（只写了 input 也算）。"""
    raw = {"zhipuai": {"models": {
        "half-free": {"cost": {"input": 0, "output": 0.5}},
        "input-only": {"cost": {"input": 0}},
    }}}
    assert modelcat._extract(raw) == {}


def test_extract_ignores_unmapped_providers():
    """没映射的平台直接不看 —— 我们建不出那种渠道，存下来只是白占磁盘。"""
    raw = {"some-random-provider": {"models": {"x": {"cost": {"input": 0, "output": 0}}}}}
    assert modelcat._extract(raw) == {}


def test_extract_uses_model_id_field_when_present():
    """模型的键名和 `id` 不一致时以 `id` 为准（目录里确实存在这种条目）。"""
    raw = {"zhipuai": {"models": {"k": {"id": "glm-real-name",
                                        "cost": {"input": 0, "output": 0}}}}}
    assert modelcat._extract(raw) == {"zhipu": ["glm-real-name"]}


def test_extract_tolerates_broken_shapes():
    """脏数据不能把整个目录搞崩（目录是外部文件，格式不归我们管）。"""
    raw = {"zhipuai": {"models": {"a": "not-a-dict", "b": {"cost": "not-a-dict"},
                                  "c": None}}}
    assert modelcat._extract(raw) == {}


def test_gemini_maps_to_google():
    """api-hub 的平台叫 gemini，models.dev 那边叫 google —— 映射错了就永远没候选。"""
    assert modelcat.PROVIDER_MAP["gemini"] == "google"
    raw = {"google": {"models": {"gemini-x": {"cost": {"input": 0, "output": 0}}}}}
    assert modelcat._extract(raw) == {"gemini": ["gemini-x"]}


# ---------- 缓存落盘 ----------

def test_update_and_load_roundtrip(tmp_path):
    modelcat.update_from_raw({"zhipuai": {"models": {
        "glm-4.7-flash": {"cost": {"input": 0, "output": 0}}}}}, ts=1234.5)
    assert os.path.exists(store.MODEL_CATALOG_PATH)
    # 清内存，强制从磁盘读
    modelcat._catalog = None
    modelcat._ts = 0.0
    assert modelcat.load_cache() == {"zhipu": ["glm-4.7-flash"]}
    assert modelcat._ts == 1234.5


def test_age_is_none_when_never_fetched():
    """没抓过必须是 None 而不是 0 —— 调用方按「None = 过期，去抓」处理，
    返回 0 会让它以为刚抓过，于是永远不去抓（免费模型雷达踩过这个坑）。"""
    modelcat._ts = 0.0
    assert modelcat.cache_age_hours() is None


def test_load_cache_survives_corrupt_file():
    with open(store.MODEL_CATALOG_PATH, "w", encoding="utf-8") as f:
        f.write("{ not json")
    modelcat._catalog = None
    assert modelcat.load_cache() == {}


# ---------- 候选计算 ----------

def test_catalog_for_unions_seed():
    """种子是离线兜底：目录里没有的（智谱 glm-4-flash 就不在 models.dev 里）
    也必须出现，否则断网时自动补全等于没有。"""
    modelcat.update_from_raw({})          # 目录空 = 完全离线
    got = modelcat.catalog_for("zhipu")
    assert "glm-4-flash" in got
    assert "glm-4.7-flash" in got


def test_catalog_for_no_duplicates():
    modelcat.update_from_raw({"zhipuai": {"models": {
        "glm-4.7-flash": {"cost": {"input": 0, "output": 0}}}}})
    got = modelcat.catalog_for("zhipu")
    assert got.count("glm-4.7-flash") == 1


def test_candidates_exclude_upstream_and_manual():
    """上游已经列了的、用户手填的，都不该再当「补」的对象 —— 否则渠道卡上
    会把同一个模型既算「上游的」又算「自动补的」。"""
    modelcat.update_from_raw({})          # 只用种子（zhipu: flash 系列 4 个）
    got = modelcat.candidates_for("zhipu",
                                  upstream_ids=["glm-4.7-flash"],
                                  exclude=["glm-4.6v-flash"])
    assert "glm-4.7-flash" not in got
    assert "glm-4.6v-flash" not in got
    assert "glm-4-flash" in got
    assert "glm-4.5-flash" in got


def test_candidates_empty_for_unknown_type():
    assert modelcat.candidates_for(None) == []
    assert modelcat.candidates_for("custom") == []
