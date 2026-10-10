"""免费模型雷达（`app.freemodels` + `providers.free_model_entries`）单元测试（离线）。

这里钉的是**判据**，不是实现细节 —— 每一条都对应一个会真实咬人的坑：

1. **免费与否只认 `pricing`**（不看名字后缀）：匿名 stealth 预览模型就是靠这条才没被漏掉；
   反过来，名字带 `:free` 但价格非 0 的（理论上不存在，但别把判据写成看名字）也不该乱进。
2. **「新面孔」的基准是本地第一次见到的时间**：抓一万次也不能把它刷新成「今天刚发现」，
   否则界面上那条「有新模型」的提示会常年闪烁 —— 提示一旦变成噪音就等于没有提示。
3. **本次没出现的模型要失活、不要删除**：OpenRouter 常把免费模型转付费后又放回来，
   删掉的话它回来时会再当一次「新面孔」，又弹一遍。
4. **缓存往返不丢首次见到时间**：它是跨重启的判据（也是这个文件存在的理由）。
"""
import time

from app import capability, freemodels, providers


def _mk(mid, prompt="0", completion="0", out=("text",), ctx=128000, bench=None, image=True):
    """造一个 OpenRouter `/models` 返回项（字段按真实返回的形状给）"""
    it = {
        "id": mid, "name": mid + " (test)", "context_length": ctx,
        "pricing": {"prompt": prompt, "completion": completion},
        "architecture": {
            "output_modalities": list(out),
            "input_modalities": ["text"] + (["image"] if image else []),
        },
    }
    if bench is not None:
        it["benchmarks"] = {"artificial_analysis": {"intelligence_index": bench}}
    return it


def _entries(*items):
    return providers.free_model_entries(list(items))


# ---------------- 收集与分类 ----------------
def test_free_entries_classify_three_kinds():
    got = {e["id"]: e["kind"] for e in _entries(
        _mk("z-ai/glm-5.3-flash:free"),
        _mk("openrouter/free"),
        _mk("stealth/space-bunny-alpha"),          # 零价却不带后缀 → 匿名测试
    )}
    assert got == {"z-ai/glm-5.3-flash:free": "free",
                   "openrouter/free": "router",
                   "stealth/space-bunny-alpha": "anon"}


def test_free_entries_skip_paid_and_non_text_output():
    got = [e["id"] for e in _entries(
        _mk("openai/gpt-9", prompt="0.000002"),         # 有价 → 不是免费
        _mk("anthropic/claude-9", completion="0.00001"),
        _mk("google/lyria-3", out=("text", "audio")),   # 音频生成 → 不配进 chat 清单
    )]
    assert got == []


def test_free_entries_carry_meta():
    e = _entries(_mk("stealth/x", bench=41.2, ctx=200000, image=True))[0]
    assert e["context"] == 200000 and e["bench"] == 41.2 and e["vision"] is True
    e2 = _entries(_mk("y-org/y", image=False))[0]
    assert e2["vision"] is False and e2["bench"] is None   # 没榜分给 None，别给 0


def test_entries_and_filter_agree():
    """雷达清单 == 渠道过滤后会保留的那批（两条判据不能各走各的）"""
    items = [_mk("a/b:free"), _mk("stealth/x"), _mk("paid/y", prompt="0.1")]
    keep, _dropped = providers.filter_free_models(
        [i["id"] for i in items], "openrouter", True,
        {e["id"].lower() for e in providers.free_model_entries(items)})
    assert sorted(keep) == ["a/b:free", "stealth/x"]


# ---------------- 「新面孔」判定 ----------------
def test_update_stamps_capture_time():
    """抓一次就必须留下「这趟几点抓的」—— `cache_age_hours()` 靠它决定要不要再拉一次。

    这条是**真实踩出来的**（2026-10-07）：`update()` 里 `_ts = now` 漏了 `global _ts`，
    于是模块级 `_ts` 永远是 0 —— 单测全绿（它们只断言 items/first_seen 这两个 dict），
    但 `cache_age_hours()` 恒为 None：后台每轮都以为「从没抓过」而白拉一次公开端点，
    界面还一直显示「本地还没有名单」。凡是「函数里给模块级标量赋值」都得有这条兜底。"""
    assert freemodels.cache_age_hours() is None            # 从没抓过
    freemodels.update(_entries(_mk("t/x:free")))
    age = freemodels.cache_age_hours()
    assert age is not None and age < 1                     # 刚抓的
    freemodels.update([], ts=time.time() - 8 * 3600)       # 用 8 小时前的时间戳抓
    assert freemodels.cache_age_hours() >= 7.9             # 「旧了」能被算出来


def test_first_seen_never_drifts():
    e = _entries(_mk("stable/model:free"))[0]
    t0 = time.time() - 10 * 86400
    freemodels.update([e], ts=t0)
    freemodels.update([e], ts=time.time())      # 十天后又抓了一次
    v = freemodels.view()
    assert v["items"][0]["first_seen"] == t0    # 首次见到时间必须原封不动
    assert v["new_count"] == 0                  # 十天前见的，不算新面孔
    assert v["total"] == 1


def test_new_faces_flagged_and_sorted_first():
    old = _entries(_mk("old/model:free"))[0]
    t0 = time.time() - 30 * 86400
    freemodels.update([old], ts=t0)
    fresh = _entries(_mk("new/model:free"))[0]
    r = freemodels.update([old, fresh], ts=time.time())
    assert r["added"] == ["new/model:free"]     # 只有真·新面孔进 added
    v = freemodels.view()
    assert v["new_count"] == 1 and v["total"] == 2
    assert v["items"][0]["id"] == "new/model:free"   # 新面孔排最前（提示的价值在新增）


def test_new_window_respects_config():
    e = _entries(_mk("mid/model:free"))[0]
    freemodels.update([e], ts=time.time() - 3 * 86400)
    assert freemodels.view(new_days=7)["new_count"] == 1     # 3 天前见的，7 天窗口内
    assert freemodels.view(new_days=2)["new_count"] == 0     # 收窄到 2 天 → 不算新


# ---------------- 失活与回收 ----------------
def test_disappeared_model_deactivated_not_deleted():
    a, b = _entries(_mk("a/x:free"), _mk("b/y:free"))
    t0 = time.time() - 30 * 86400
    freemodels.update([a, b], ts=t0)
    freemodels.update([a], ts=t0 + 3600)             # b 这轮没出现 → 下架
    v = freemodels.view()
    assert [i["id"] for i in v["items"]] == ["a/x:free"]   # 清单只列在架的
    assert v["gone"] == 1
    freemodels.update([a, b], ts=t0 + 7200)          # b 又回来了
    v2 = freemodels.view()
    assert v2["gone"] == 0
    assert v2["new_count"] == 0                      # 回来仍是老面孔，不该再提示一次


def test_gc_drops_long_gone():
    e = _entries(_mk("ancient/model:free"))[0]
    freemodels.update([e], ts=time.time() - (freemodels.GONE_KEEP_DAYS + 10) * 86400)
    freemodels.update([], ts=time.time())            # 全部失活且超过保留期 → 清掉
    v = freemodels.view()
    assert v["total"] == 0 and v["gone"] == 0


# ---------------- 缓存往返 ----------------
def test_cache_roundtrip_keeps_first_seen_and_kind():
    e = _entries(_mk("stealth/round-trip", bench=33.3))[0]
    t0 = time.time() - 3 * 86400
    freemodels.update([e], ts=t0)
    assert freemodels.save_cache() is True
    assert freemodels.save_cache() is False          # 已落盘 → 不脏就不写（少一次写盘）
    freemodels._items.clear(); freemodels._first_seen.clear(); freemodels._ts = 0.0
    info = freemodels.load_cache()
    v = freemodels.view()
    assert info["loaded"] and info["total"] == 1
    assert v["items"][0]["id"] == "stealth/round-trip"
    assert v["items"][0]["first_seen"] == t0         # 重启后仍是老面孔
    assert v["items"][0]["kind"] == "anon"
    assert v["new_count"] == 1
    assert freemodels.cache_age_hours() is not None        # 抓取时间也得一起活下来


def test_empty_cache_is_not_an_error():
    info = freemodels.load_cache()                   # 文件不存在
    assert info["loaded"] is False and info["total"] == 0
    v = freemodels.view()
    assert v["never"] is True                        # 界面据此说「本地还没有名单」
    assert v["items"] == [] and v["total"] == 0


# ---------------- 匿名测试模型 → 默认智能档（2026-10-07 加） ----------------
# 背景：OpenRouter 把新模型以「匿名 stealth 预览版」零价放出（pricing 全 0 却不带 `:free`
# 后缀，如 `inclusionai/ling-3.1-flash`）。这类名字对名字启发式完全陌生 → 走
# 「无名家族 → 默认轻量」被压成 1 档，界面上看着像小模型（用户实测反馈要改）。
def test_anon_models_default_to_smart_tier():
    """匿名测试模型默认智能档 —— 但只给 anon 开这条通道。

    对照组是关键：同样是「无名家族 + 无榜分」，普通 `:free` 模型仍走老规则（轻量档）。
    若判据写成「认不出名字就抬档」，会把几百个野生小模型一起抬到智能档。
    """
    freemodels.update(_entries(
        _mk("inclusionai/ling-3.1-flash"),          # 零价不带后缀 → anon
        _mk("nobody/whatever-thing:free"),          # 带 :free → 普通免费版
    ))
    assert capability.tier_of("inclusionai/ling-3.1-flash") == 3
    assert capability.tier_of("nobody/whatever-thing:free") == 1   # 没被连带抬档


def test_anon_tier_matches_across_channel_id_forms():
    """本机渠道返回的 id 写法未必和 OpenRouter 目录一致（换前缀 / 带 :free）→ 必须归一化后比。"""
    freemodels.update(_entries(_mk("vendor/quantum-7-alpha")))
    assert capability.tier_of("vendor/quantum-7-alpha") == 3
    assert capability.tier_of("other/quantum-7-alpha") == 3       # 换厂商前缀仍认得出
    assert capability.is_anon("quantum-7-alpha:free") is True     # 带变体后缀也认得出


def test_bench_score_wins_over_anon_default():
    """有 AA 榜分就以榜分为准 —— 那是实测值，「默认」不该压过它。

    （所以 anon 判定放在 `tier_of` 的榜分之后：走到那儿才说明真没榜分。）
    """
    capability.update_bench_scores({"scored": 5.0})               # 榜分明显偏低
    freemodels.update(_entries(_mk("stealth/scored")))
    assert capability.tier_of("stealth/scored") == 1


def test_anon_tier_survives_cache_reload():
    """重启（`load_cache`）也要把名单推一次 —— 少这条就会出现「重启前智能档、重启后轻量档」。"""
    freemodels.update(_entries(_mk("stealth/reload-me")))
    assert freemodels.save_cache() is True
    # 模拟进程重启：内存全空。⚠️ 档位集合要手动清 —— 它是「推」进来的，清 `_items`
    # 不会连带清掉（这正是推模式的语义：谁有名单谁登记，见 capability.set_anon_models）。
    freemodels._items.clear(); freemodels._first_seen.clear(); freemodels._ts = 0.0
    capability.set_anon_models([])
    assert capability.tier_of("stealth/reload-me") == 1            # 新进程起步：只剩名字启发式
    freemodels.load_cache()
    assert capability.tier_of("stealth/reload-me") == 3            # 恢复名单后档位也回来


def test_anon_flag_persists_after_model_leaves_catalog():
    """下架后仍保留 anon 标记。

    只认「在架」的话，OpenRouter 把测试模型转正/撤下时档位会在 3↔1 之间来回跳
    （6 小时一个抓取周期），看起来像 bug；而且「曾经是前沿灰度模型」这个事实不会过期。
    真要说错，也只是给一个前沿模型多算了档位 —— 比来回跳好。
    """
    freemodels.update(_entries(_mk("stealth/went-paid")))
    freemodels.update([])                                          # 本次没出现 → 失活
    assert freemodels.view()["total"] == 0                         # 已不在架
    assert capability.is_anon("stealth/went-paid") is True         # 但档位标记还在


def test_anon_list_is_replaced_not_merged():
    """每次推送是**整份替换**，不是追加 —— 否则清空的名单永远清不掉（旧模型粘在集合里）。"""
    freemodels.update(_entries(_mk("stealth/old-one")))
    assert capability.is_anon("stealth/old-one") is True
    assert capability.set_anon_models([]) == 0
    assert capability.is_anon("stealth/old-one") is False
