"""chat 方言：恒等映射。

客户端本来就发 OpenAI chat completions 的 body，所以这里只做**最小归一**，不做任何翻译：

- 请求方向：把 body 换成一份**浅拷贝**再交给 `main._chat_v1`。这不是洁癖 ——
  `_chat_v1` 会就地改写 `body["model"]`（别名/上游模型名切换，`main.py:522`），
  不拷贝的话调用方手里那份 body 会被悄悄改掉。
- 响应方向：原样返回（`_chat_v1` 产出的就是 chat 形状）。

放在注册表里是为了让"三种方言"这件事在代码里是齐的：加第四种方言时，
这里就是那个"不需要翻译"的参照物。
"""

from __future__ import annotations


def to_chat(body: dict, cfg: dict | None = None) -> tuple[dict, dict]:
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("缺少 model 字段")
    chat_body = dict(body)
    chat_body["model"] = model.strip()
    return chat_body, {"model": chat_body["model"]}


async def convert(resp, ctx: dict):
    """chat 入口不需要任何回包翻译"""
    return resp
