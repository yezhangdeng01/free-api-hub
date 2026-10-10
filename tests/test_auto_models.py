"""自动补全（`gateway.auto_add_models` / `_probe_candidates`）。

要守的主张：**上游 /models 不列出来的免费模型，用户不填也该能用上，但必须先探活**。
所以每个「会加进来」的用例都配了对照：探不到的不加、关掉开关不加、上游真挂了也不该被掩盖。

项目没装 pytest-asyncio（打桩规范一贯用 `asyncio.run`），所以这里不用 mark。
"""
import asyncio
import os

import pytest

from app import gateway, modelcat, providers

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "frontend", "index.html")


@pytest.fixture(scope="module")
def html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


class _Resp:
    """假响应：默认给一个「像样」的 200 体 —— 探活现在要解析 `.json()`。"""

    def __init__(self, status, body=None):
        self.status_code = status
        self._body = ({"choices": [{"message": {"content": ""}}]}
                      if body is None else body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Client:
    """假 httpx 客户端：按模型名给状态码，并记下被问过哪些模型。"""

    def __init__(self, mapping, boom=(), bodies=None):
        self.mapping = mapping
        self.boom = set(boom)
        self.bodies = bodies or {}
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        m = json["model"]
        self.calls.append(m)
        if m in self.boom:
            raise RuntimeError("connection reset")
        return _Resp(self.mapping.get(m, 404), self.bodies.get(m))


@pytest.fixture(autouse=True)
def _clean_gateway():
    """`get_cs` 是按渠道 id 缓存的：不清的话上一个测试的 cs.models 会累积到下一个。"""
    gateway.channels.clear()
    yield
    gateway.channels.clear()


def _chan(**kw):
    ch = {"id": "ch_x", "name": "测试渠道", "type": "zhipu", "api_key": "k",
          "base_url": "https://example.invalid/api/paas/v4", "enabled": True}
    ch.update(kw)
    return ch


@pytest.fixture
def _marks(monkeypatch):
    """拦掉 mark_result：这里只验「加不加」，别顺手去动 gateway 的全局状态表
    （cooldown / channel_last_ok 那些跨测试串起来极难查）。返回记录列表。"""
    seen = []
    monkeypatch.setattr(gateway, "mark_result",
                        lambda m, cid, ok, *a, **k: seen.append((m, ok)))
    return seen


def _cands(*models):
    return lambda t, up=(), ex=(): list(models)


# ---------- 探活判据 ----------

def test_only_really_working_models_are_added(monkeypatch, _marks):
    """只并入**这次真调通**的：200 进，429 不进，404 也进不了。

    这是 2026-10-07 用户实测反馈后收紧的判据。旧版把 429 也算「存在」，
    于是 `glm-4.7-flash` 被自动补进池子，用户点测试却报「不可用」——
    **「忙」不等于「可用」**：自动补全的意义是拿来就能调，不是让用户自己试错。
    """
    monkeypatch.setattr(gateway, "_CAND_RETRY_DELAY", 0)
    monkeypatch.setattr(modelcat, "candidates_for",
                        _cands("glm-4-flash", "glm-4.7-flash", "glm-ghost"))
    client = _Client({"glm-4-flash": 200, "glm-4.7-flash": 429})
    got = asyncio.run(gateway.auto_add_models(client, _chan(), [], []))
    assert got == ["glm-4-flash"]
    assert _marks == [("glm-4-flash", True)], "429 不该被标绿"


def test_drops_403_and_400(monkeypatch, _marks):
    """403（无权）/ 400（模型名不对）都不是「存在」：加进去只会给用户一个假模型。"""
    monkeypatch.setattr(modelcat, "candidates_for", _cands("a", "b", "c"))
    client = _Client({"a": 403, "b": 400, "c": 200})
    assert asyncio.run(gateway.auto_add_models(client, _chan(), [], [])) == ["c"]


def test_5xx_is_busy_not_alive(monkeypatch, _marks):
    """5xx 现在按「忙」处理，不并入 —— 上游抖动时给用户一个调不通的模型没有意义。"""
    monkeypatch.setattr(gateway, "_CAND_RETRY_DELAY", 0)
    monkeypatch.setattr(modelcat, "candidates_for", _cands("m1"))
    client = _Client({"m1": 503})
    assert asyncio.run(gateway.auto_add_models(client, _chan(), [], [])) == []


def test_busy_gets_one_retry(monkeypatch, _marks):
    """429/5xx 先重试一次再下结论：单次抖动不该被当成「忙」。"""
    monkeypatch.setattr(gateway, "_CAND_RETRY_DELAY", 0)
    monkeypatch.setattr(modelcat, "candidates_for", _cands("m1"))
    client = _Client({"m1": 429})
    assert asyncio.run(gateway.auto_add_models(client, _chan(), [], [])) == []
    assert client.calls.count("m1") == 2, "该重试一次（共 2 次请求）"


def test_retry_rescues_a_flaky_model(monkeypatch, _marks):
    """第一次 429、重试就 200 → 那只是抖，该收下。"""
    monkeypatch.setattr(gateway, "_CAND_RETRY_DELAY", 0)
    monkeypatch.setattr(modelcat, "candidates_for", _cands("m1"))

    class Flaky:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append(json["model"])
            return _Resp(429 if len(self.calls) == 1 else 200)

    c = Flaky()
    assert asyncio.run(gateway.auto_add_models(c, _chan(), [], [])) == ["m1"]
    assert len(c.calls) == 2


def test_200_with_error_object_is_not_added(monkeypatch, _marks):
    """200 也不能无条件信：上游可能拿 200 包一个错误对象。"""
    monkeypatch.setattr(gateway, "_CAND_RETRY_DELAY", 0)
    monkeypatch.setattr(modelcat, "candidates_for", _cands("m1"))
    client = _Client({"m1": 200}, bodies={"m1": {"error": {"message": "model not found"}}})
    assert asyncio.run(gateway.auto_add_models(client, _chan(), [], [])) == []


def test_busy_cache_expires_sooner_than_ok():
    """「忙」只压 30 分钟，「可用」压 6 小时 —— 忙是时段性的，压太久等于把模型拉黑。"""
    assert gateway._CAND_BUSY_TTL < gateway._CAND_OK_TTL
    assert gateway._CAND_BUSY_TTL <= 30 * 60
    assert gateway._CAND_OK_TTL >= 6 * 3600


# ---------- 成本与开关 ----------

def test_switch_off_sends_nothing(monkeypatch, _marks):
    """关掉开关就一个请求都不发（用户明确表示不想要）。"""
    monkeypatch.setattr(modelcat, "candidates_for", _cands("glm-4-flash"))
    client = _Client({"glm-4-flash": 200})
    assert asyncio.run(gateway.auto_add_models(client, _chan(auto_models=False), [], [])) == []
    assert client.calls == []


def test_result_is_reused_from_cache(monkeypatch, _marks):
    """探活结果要复用：健康检查 30 分钟一轮，不该每轮都拿免费额度去重探。"""
    monkeypatch.setattr(modelcat, "candidates_for", _cands("glm-4-flash"))
    client = _Client({"glm-4-flash": 200})
    ch = _chan()
    assert asyncio.run(gateway.auto_add_models(client, ch, [], [])) == ["glm-4-flash"]
    assert asyncio.run(gateway.auto_add_models(client, ch, [], [])) == ["glm-4-flash"]
    assert client.calls == ["glm-4-flash"], "第二轮命中了缓存就不该再发请求"


def test_network_error_is_not_cached(monkeypatch, _marks):
    """连不上是**本机**问题（代理抖、断网），不能写进缓存 ——
    否则代理抽一下，那个模型就被拉黑 6 小时，用户重启也不一定恢复。"""
    monkeypatch.setattr(modelcat, "candidates_for", _cands("glm-4-flash"))
    client = _Client({}, boom=["glm-4-flash"])
    assert asyncio.run(gateway.auto_add_models(client, _chan(), [], [])) == []
    assert gateway._cand_cache == {}
    asyncio.run(gateway.auto_add_models(client, _chan(), [], []))
    assert len(client.calls) == 2, "没缓存 → 两轮都该重试（每轮 1 次）"


def test_candidates_are_capped(monkeypatch, _marks):
    """候选上限：上游 /models 一挂，候选就变成整份目录（NIM 100+ 个），不能全探。"""
    many = ["m%d" % i for i in range(50)]
    monkeypatch.setattr(modelcat, "candidates_for", lambda t, up=(), ex=(): many)
    client = _Client({m: 200 for m in many})
    got = asyncio.run(gateway.auto_add_models(client, _chan(), [], []))
    assert len(client.calls) == gateway._CAND_MAX
    assert len(got) == gateway._CAND_MAX


# ---------- 端到端（含上游列表） ----------

def test_refresh_merges_auto_into_models(monkeypatch, _marks):
    """上游列表 + 自动补的 = 渠道模型列表，`auto_added` 如实记录来源。"""
    async def fake_fetch(client, base, key, ch_type=None, free_only=None):
        return ["glm-4.5"]
    monkeypatch.setattr(providers, "fetch_models", fake_fetch)
    monkeypatch.setattr(modelcat, "candidates_for", _cands("glm-4-flash"))
    cs = asyncio.run(gateway.refresh_channel(_Client({"glm-4-flash": 200}), _chan()))
    assert cs.valid is True
    assert cs.models == ["glm-4-flash", "glm-4.5"]
    assert cs.auto_added == ["glm-4-flash"]
    assert cs.list_error is None


def test_refresh_auto_rescues_missing_models_endpoint(monkeypatch, _marks):
    """上游根本没有 /models（Cloudflare Workers AI 实测 405）时，
    自动补全要能独自把渠道撑成「可用」，否则整条渠道会被判连接失败直接停用。"""
    async def boom(client, base, key, ch_type=None, free_only=None):
        raise RuntimeError("HTTP 405 Method Not Allowed")
    monkeypatch.setattr(providers, "fetch_models", boom)
    monkeypatch.setattr(modelcat, "candidates_for", _cands("glm-4-flash"))
    cs = asyncio.run(gateway.refresh_channel(_Client({"glm-4-flash": 200}), _chan()))
    assert cs.valid is True
    assert cs.models == ["glm-4-flash"]
    assert "405" in cs.list_error


def test_refresh_without_candidates_keeps_old_behaviour(monkeypatch, _marks):
    """对照组：没候选时一切照旧 —— 上游 401 就是连接失败，
    不能被兜底逻辑悄悄「救活」（那会掩盖 Key 失效这种真问题）。"""
    async def boom(client, base, key, ch_type=None, free_only=None):
        raise RuntimeError("HTTP 401 Unauthorized")
    monkeypatch.setattr(providers, "fetch_models", boom)
    monkeypatch.setattr(modelcat, "candidates_for", _cands())
    cs = asyncio.run(gateway.refresh_channel(_Client({}), _chan()))
    assert cs.valid is False
    assert "401" in cs.error
    assert cs.auto_added == []


def test_refresh_upstream_wins_no_duplicates(monkeypatch, _marks):
    """上游已经列了的模型不会再被「自动补」一遍（并集，不是追加）。"""
    async def fake_fetch(client, base, key, ch_type=None, free_only=None):
        return ["glm-4-flash", "glm-4.5"]
    monkeypatch.setattr(providers, "fetch_models", fake_fetch)
    # 这里**不** mock candidates_for：要的就是真实候选表（智谱种子）跟上游列表求并集的
    # 真实效果。上游已经给了 glm-4-flash，候选里就必须没有它。
    cs = asyncio.run(gateway.refresh_channel(
        _Client({"glm-4-flash": 200, "glm-4.5-flash": 200, "glm-4.6v-flash": 200}),
        _chan(type="zhipu")))
    assert cs.models.count("glm-4-flash") == 1
    assert "glm-4-flash" not in cs.auto_added
    assert "glm-4.5-flash" in cs.auto_added      # 这个确实不在上游列表里 → 该被补


# ---------- 前端契约 ----------

def test_extra_input_shares_row_with_save_button(html):
    """用户 2026-10-07 的明确要求：额外模型框和保存按钮**同一行**，且排在保存**前面**。

    中间夹一个 `</div>` 就说明它被拆回独立的一行了 —— 那正是这次要改掉的布局。
    """
    i_extra = html.index('id="ch-extra"')
    i_save = html.index('id="ch-save"')
    assert i_extra < i_save, "额外模型框要排在保存按钮前面"
    assert "</div>" not in html[i_extra:i_save], \
        "额外模型框和保存按钮之间出现了 </div>：它们不在同一行"


def test_channel_form_has_auto_toggle(html):
    assert 'id="ch-auto"' in html, "渠道表单缺「自动补全免费模型」开关"


def test_form_body_sends_auto_models(html):
    body = html[html.index("function formBody()"):]
    body = body[:body.index('$("#ch-save")')]
    assert "auto_models" in body, "formBody() 没带上 auto_models，关掉开关也不生效"


def test_edit_and_reset_touch_auto_models(html):
    edit = html[html.index("function editCh("):]
    edit = edit[:edit.index("async function testCh(")]
    assert "ch.auto_models" in edit, "editCh() 没回填自动补全开关，编辑一次就把它改回默认了"
    reset = html[html.index("function resetForm("):]
    reset = reset[:reset.index("function editCh(")]
    assert "#ch-auto" in reset, "resetForm() 没复位自动补全开关，会串到下一条渠道"


def test_channel_card_shows_auto_added(html):
    """自动补进来的模型要在渠道卡上说明来源，否则用户会奇怪「我没填怎么多了模型」。"""
    assert "auto_added" in html, "渠道卡没显示自动补全的结果"

