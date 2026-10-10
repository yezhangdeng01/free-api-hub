"""「额外模型」渠道字段测试（2026-10-07 加）。

背景（两个真实场景，实测于 2026-10-07）：
  ① 智谱 `glm-4-flash` / `glm-4.7-flash` 官方永久免费、`chat/completions` 直接 200，
     但智谱 `GET /models` 只返回 11 个主力/付费模型 —— 不含它俩，于是永远进不了
     `cs.models`（渠道模型列表完全来自上游 /models）；
  ② Cloudflare Workers AI 的 OpenAI 兼容端点**只有 /chat/completions 与 /embeddings**，
     压根没有 /models —— 不手填就一个模型都拉不到，而且会因为 /models 404 被判
     「连接失败」整条渠道停用。

所以这个字段要同时满足三件事：
  · 归一化：中英文逗号 / 分号 / 空白换行都能当分隔符，去重保序；
  · 合并：与上游返回的列表取并集（上游优先，不覆盖）；
  · 兜底：上游 /models 失败但手填非空 → 渠道仍算可用（否则手填等于白填）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gateway, providers  # noqa: E402
from app.main import _validate_channel  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean_gateway():
    gateway.channels.clear()
    yield
    gateway.channels.clear()


def _chan(cid="c1", extra=None, **kw):
    ch = {"id": cid, "name": cid, "type": "custom", "base_url": "http://x.invalid/v1",
          "api_key": "k", "enabled": True}
    if extra is not None:
        ch["extra_models"] = extra
    ch.update(kw)
    return ch


# ---------------- 1. 表单/导入的归一化（main._validate_channel） ----------------
def _body(**kw):
    b = {"type": "custom", "name": "x", "base_url": "http://x.invalid/v1", "api_key": "k"}
    b.update(kw)
    return b


@pytest.mark.parametrize("raw,expect", [
    ("glm-4-flash, glm-4.7-flash", ["glm-4-flash", "glm-4.7-flash"]),      # 英文逗号
    ("glm-4-flash，glm-4.7-flash", ["glm-4-flash", "glm-4.7-flash"]),      # 中文逗号
    ("glm-4-flash;glm-4.7-flash", ["glm-4-flash", "glm-4.7-flash"]),       # 分号
    ("glm-4-flash\nglm-4.7-flash\n", ["glm-4-flash", "glm-4.7-flash"]),    # 换行
    ("  glm-4-flash  ", ["glm-4-flash"]),                                  # 两侧空白
    ("a,a,b", ["a", "b"]),                                                 # 去重保序
    ("a,,,b", ["a", "b"]),                                                 # 空项丢掉
    ("@cf/meta/llama-3.1-8b-instruct", ["@cf/meta/llama-3.1-8b-instruct"]),  # Cloudflare 全名
])
def test_extra_models_normalized(raw, expect):
    ch, err = _validate_channel(_body(extra_models=raw))
    assert err is None
    assert ch["extra_models"] == expect


def test_extra_models_empty_clears_field():
    """空串 = 清空。编辑渠道时把输入框删空，必须能真的删掉，而不是被空列表覆盖成 None。"""
    ch, err = _validate_channel(_body(extra_models="  "))
    assert err is None
    assert "extra_models" not in ch


def test_extra_models_absent_keeps_field_out():
    ch, err = _validate_channel(_body())
    assert err is None
    assert "extra_models" not in ch


def test_extra_models_accepts_list():
    """配置导入（/api/config/import）走同一函数，JSON 里写的是数组。"""
    ch, err = _validate_channel(_body(extra_models=["glm-4-flash", " glm-4.7-flash "]))
    assert err is None
    assert ch["extra_models"] == ["glm-4-flash", "glm-4.7-flash"]


# ---------------- 2. 运行时读取（gateway.extra_models_of） ----------------
def test_extra_models_of_strips_and_dedups():
    assert gateway.extra_models_of(_chan(extra=[" a ", "a", "", None, "b"])) == ["a", "b"]
    assert gateway.extra_models_of(_chan()) == []


# ---------------- 3. 并入上游列表 ----------------
def _patch_fetch(monkeypatch, result):
    """把 providers.fetch_models 换成固定返回 / 固定抛错的假实现。

    项目没装 pytest-asyncio（打桩规范一贯用 asyncio.run），所以这里不动 mark。"""
    async def fake(client, base, key, ch_type=None, free_only=None):
        if isinstance(result, Exception):
            raise result
        return list(result)
    monkeypatch.setattr(providers, "fetch_models", fake)


def test_refresh_merges_extra_into_models(monkeypatch):
    """上游返回的 + 手填的 = 并集，且排序稳定。"""
    _patch_fetch(monkeypatch, ["glm-4.5", "glm-5.3"])
    ch = _chan(extra=["glm-4-flash", "glm-4.5"])
    cs = asyncio.run(gateway.refresh_channel(None, ch))
    assert cs.valid is True
    # 并集（glm-4.5 上游已有，不重复）。顺序是 sorted 的字面序：'-'(0x2D) 排在 '.'(0x2E) 前
    assert cs.models == ["glm-4-flash", "glm-4.5", "glm-5.3"]
    assert cs.list_error is None


def test_refresh_falls_back_to_extra_when_listing_fails(monkeypatch):
    """上游没有 /models（Cloudflare 就是这种）→ 只要手填了模型，渠道仍算可用。

    若这里判成 valid=False，gateway 所有取模型的路径（都要求 cs.valid is True）
    都会跳过它，用户手填的模型一个也用不上。"""
    _patch_fetch(monkeypatch, RuntimeError("HTTP 404 Not Found"))
    ch = _chan(extra=["@cf/meta/llama-3.1-8b-instruct", "@cf/openai/gpt-oss-120b"])
    cs = asyncio.run(gateway.refresh_channel(None, ch))
    assert cs.valid is True, "有手填模型却被判连接失败，手填等于白填"
    assert cs.models == ["@cf/meta/llama-3.1-8b-instruct", "@cf/openai/gpt-oss-120b"]
    assert cs.error is None
    assert "404" in cs.list_error, "兜底原因要记下来，界面要显示，否则用户以为扫描坏了"


def test_refresh_still_fails_without_extra(monkeypatch):
    """对照组：没填额外模型时，/models 失败仍然是「连接失败」（老行为不能变）。"""
    _patch_fetch(monkeypatch, RuntimeError("HTTP 401 Unauthorized"))
    cs = asyncio.run(gateway.refresh_channel(None, _chan()))
    assert cs.valid is False
    assert "401" in cs.error
    assert cs.list_error is None


def test_refresh_success_clears_stale_list_error(monkeypatch):
    """上游恢复后，上一次失败的 list_error 必须清掉（否则界面一直挂着旧提示）。"""
    ch = _chan(extra=["m1"])
    _patch_fetch(monkeypatch, RuntimeError("HTTP 404"))
    cs = asyncio.run(gateway.refresh_channel(None, ch))
    assert cs.list_error

    _patch_fetch(monkeypatch, ["m1", "m2"])
    cs = asyncio.run(gateway.refresh_channel(None, ch))
    assert cs.list_error is None
    assert cs.models == ["m1", "m2"]


# ---------------- 4. 前端契约（单文件，靠隐式约定连接） ----------------
INDEX = os.path.join(ROOT, "frontend", "index.html")


@pytest.fixture(scope="module")
def html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def test_channel_form_has_extra_models_field(html):
    assert 'id="ch-extra"' in html, "渠道表单缺 #ch-extra 输入框，用户没法手填模型"


def test_form_body_sends_extra_models(html):
    """表单 → 后端：不发送的话后端 never 收到，字段永远是空的。"""
    body = html[html.index("function formBody()"):]
    body = body[:body.index("$(\"#ch-save\")")]
    assert "extra_models" in body, "formBody() 没带上 extra_models，保存时手填内容会被丢掉"


def test_edit_and_reset_touch_extra_models(html):
    """编辑要回填、取消要清空 —— 不然「编辑别的渠道」会把上一条的额外模型带过去。"""
    edit = html[html.index("function editCh("):]
    edit = edit[:edit.index("async function testCh(")]
    assert 'ch.extra_models' in edit, "editCh() 没回填额外模型，编辑保存会把它们清掉"
    reset = html[html.index("function resetForm("):]
    reset = reset[:reset.index("function editCh(")]
    assert '#ch-extra' in reset, "resetForm() 没清空额外模型输入框，会串到下一条渠道"
