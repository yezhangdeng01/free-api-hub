"""下游协议方言注册表。

网关的**内部形状永远是 chat completions**（`main._chat_v1` 的入参就是它）。这里管的是
"客户端说的是哪种方言"：每种方言实现两个函数，两个方向都在同一个模块里，

    to_chat(body, cfg) -> (chat_body, ctx)    请求：方言 body → chat body
    convert(resp, ctx) -> Response/JSONResponse  响应：chat 的响应/SSE → 方言的响应/事件流

渠道路由、冷却分类、失败换路、会话粘性、用量记账**都不在方言层**——翻译完的请求照旧交给
`main._chat_v1`，所以加一种方言不会碰到任何渠道逻辑。

现在三种方言：

    chat       恒等映射（本来就是 chat，只做最小归一）
    responses  OpenAI Responses API（`/v1/responses`）
    messages   Anthropic Messages API（`/v1/messages`）

加第四种方言的做法：在这个包里加一个模块，实现上面两个函数，然后在下面 `_DIALECTS` 注册一行，
最后在 `main.py` 挂一个端点（两行）。**别在方言模块里 import `app.main`**（会循环 import，
PyInstaller 打包也会跟着出问题）。
"""
from __future__ import annotations

from . import chat, messages, responses  # noqa: F401

#: 方言名 → 实现模块。键名就是"客户端说的是什么协议"。
_DIALECTS = {
    "chat": chat,
    "responses": responses,
    "messages": messages,
}


def get(name: str):
    """按名字取方言实现；不认识的名字抛 KeyError（调用方负责转成 400）"""
    try:
        return _DIALECTS[name]
    except KeyError:
        raise KeyError(f"未知方言 {name!r}，已注册：{', '.join(sorted(_DIALECTS))}") from None


def names() -> list:
    return sorted(_DIALECTS)


__all__ = ["chat", "responses", "messages", "get", "names"]
