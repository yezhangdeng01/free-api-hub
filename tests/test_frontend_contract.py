"""前端契约测试（离线，只读 frontend/index.html 这个单文件）。

存在理由：`frontend/index.html` 是单文件、无构建，模板字符串和 JS 之间靠**隐式约定**
（类名 / data 属性）连接。改模板时顺手改了类名，JS 会静默失效 —— 界面不报错，
只是「点了测试没反应」，最难查的一类 bug（2026-09-26 的「实测结论写回行内」
就是这么一套约定：`tr[data-row-model]` + `.st-chip`）。

这里只钉**跨前后端的那几条契约**，不做 HTML 结构快照（那种测试一改版就红，没人维护）。

2026-09-26 第三轮追加的约定：行内状态是「**内存为真、DOM 只是投影**」——
因为整张表会被 `loadOverview()` 重建（且脚本末尾有个 30 秒的静默刷新），
把状态只写在 DOM 上会丢。所以 `renderModels()` 末尾必须重放一次。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "frontend", "index.html")


@pytest.fixture(scope="module")
def html():
    assert os.path.isfile(INDEX), f"找不到前端文件：{INDEX}"
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _slice(html: str, start: str, end: str) -> str:
    """取 [start, end) 之间的源码片段（两端都是字面量标记）。"""
    assert start in html, f"找不到起点标记：{start}"
    body = html[html.index(start):]
    assert end in body, f"找不到终点标记：{end}"
    return body[:body.index(end)]


# ---------------- 行内测试状态（2026-09-26 第二轮 / 第三轮） ----------------
# 「会消失的东西不该是唯一证据」：测试结论写回这一行，是主证据（浮层已在第三轮撤掉）。
# 钩子缺一不可 —— 模板不给 data-row-model，JS 就找不到行；不给 .st-chip，就没处写结论。
def test_row_widget_hooks_exist(html):
    assert 'data-row-model="${esc(m.id)}"' in html, "模型行的 <tr> 少了 data-row-model，行内状态写不回去"
    assert '<span class="st-chip">' in html, "状态 chip 槽位（.st-chip）不见了，实测结论没有落点"
    assert 'querySelector(".st-chip")' in html, "JS 没按 .st-chip 找落点，与模板对不上"


def test_row_state_functions_exist(html):
    for fn in ("_rowEl", "_rowParts", "_rowPaintTesting", "_rowPaintResult",
               "_rowRepaint", "_rowTestBusy", "_rowTestStop", "_rowTestDone"):
        assert re.search(r"function\s+%s\s*\(" % fn, html), f"缺函数 {fn}"
    # 状态必须存放在内存 Map 里（DOM 会被整表重建），而不是只写在 DOM 上。
    # 旧版用 `_rowTestTimers`（每个模型一个 interval）—— 那是「读秒在走、圆点不呼吸」的根源。
    assert "const _rowTestingAt = new Map()" in html, "缺「测试进行中」的内存登记表"
    assert "const _rowResult = new Map()" in html, "缺「最近实测结论」的内存登记表"
    assert "let _rowPump = null" in html, "秒表 interval 没有单例句柄，会退回到每行一个定时器"


def test_rows_repaint_after_table_rebuild(html):
    """整表重建后必须重放行内状态（这条是用户报的 bug 的回归用例）。

    少了 `renderModels()` 末尾的 `_rowRepaint()`：脚本末尾那个 30 秒的静默
    `loadOverview()` 会把带 `testing` 类的行整个换掉，而秒表仍在往新 chip 里写 ——
    界面就成了「还在读秒、圆点却不呼吸」（用户 15:1x 报的）。
    """
    body = _slice(html, "function renderModels(){", '$("#model-search").oninput')
    assert "_rowRepaint();" in body, "renderModels() 重建完表没重放行内状态（会重现「读秒但圆点不呼吸」）"
    assert "setInterval(() => loadOverview().catch(()=>{}), 30000)" in html, \
        "那个 30 秒静默刷新不见了 —— 它正是本 bug 的成因，删掉的话请一并复核这条用例"


def test_row_state_css_exists(html):
    # 「测试中」的视觉信号：圆点呼吸 + chip 文字，靠这三个类名
    assert ".light-dot.testing" in html and "@keyframes dot-breathe" in html
    assert ".tag-chip.testing" in html
    assert "tr.row-testing" in html


def test_testmodel_wires_row_state(html):
    """testModel 必须真的调用了这组函数（写了函数但没接上 = 白写）。"""
    body = _slice(html, "async function testModel", "#pg-test-all")
    assert "_rowTestBusy(model)" in body
    assert "_rowTestDone(model" in body


def test_testmodel_does_not_use_toast(html):
    """第三轮起：**测试结果只写行内，不弹浮层**。

    用户原话：「既然行内已经显示状态了，悬浮的那个就不用留了」。
    同时要求刷新失败时仍有落点 —— 那条提醒必须留在行内，否则「页面其它数字可能是旧的」
    这件事就没地方说了。
    """
    body = _slice(html, "async function testModel", "#pg-test-all")
    assert "toast(" not in body, "testModel 里又出现 toast 了（用户已要求撤掉浮层）"
    assert "列表未刷新" in body, "刷新失败的行内提醒不见了：不弹浮层时它是唯一的落点"


def test_toast_is_anchored_not_only_bottom(html):
    """浮层仍要能锚到按钮（现在只剩批量/扫描完成这类「行内没有落点」的汇总在用它）。"""
    assert "#toast.anchored" in html
    assert "function _toastPlace" in html
    # 定时器必须句柄化：否则连发两条时，第一条的定时器会把第二条提前关掉
    assert "let _toastTimer" in html or "var _toastTimer" in html
    assert "clearTimeout(_toastTimer)" in html


# ---------------- 渠道卡片：冷却原因（2026-09-27） ----------------
def test_channel_card_uses_backend_cool_reason(html):
    """渠道卡片的「冷却中」必须显示后端给的**原因**，不能再写死一句通用文案。

    背景：用户报「魔搭额度用完了，但状态一会就跳回可用」（主因是冷却只有 30 分钟，
    已改到次日 00:05）。顺带要能读出**为什么**停 —— 旧版写死的
    「限流熔断中（账号级限流）」分不出「额度真没了」和「同时撞了限速」，
    而两者的应对完全不同（去领魔粒/等明天 vs 等十分钟）。

    后端字段是 `/api/overview` 的 `channels[].cool_reason`（由
    `tests/test_api.py::test_overview_exposes_channel_cool_reason` 从另一端钉住）。
    """
    assert "ch.cool_reason" in html, "渠道卡片没用后端给的冷却原因（cool_reason）"
    assert "限流熔断中（账号级限流）" not in html, "写死的通用冷却文案又回来了"
