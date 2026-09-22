"""Anthropic Messages API 方言（`/v1/messages`）→ 内部 chat completions 翻译层。

对外：让只说 Anthropic 协议的客户端（Claude Code、Anthropic SDK、各种 agent 框架）
能用上这个网关；对内：把 `/v1/messages` 的请求译成 chat body、把 chat 的回包译回
Anthropic 的响应对象 / SSE 事件流。

**渠道路由不在这里**：翻译完的请求交给 `main._chat_v1`，渠道选择、冷却分类、failover、
会话粘性、用量记账全部沿用 chat 入口那一套 —— 这里只做协议映射（和 `dialects/responses.py`
同一个骨架、同一个 `to_chat` / `convert` 约定）。

三条设计约束（与 responses 方言保持一致）：

1. **无状态**：不保存任何会话；客户端每轮把完整对话放进 `messages`。
2. **失败不致命**：上游少个字段、`delta` 写法奇怪，都不该让整条请求崩掉。认不出来的部分降级丢弃。
   只有「客户端自己写错了」才抛 `ValueError`（→ 400）：缺 model、messages 不是数组、messages 为空。
3. **不做字段清洗**：与 chat 不冲突的字段原样透传。

两条 Anthropic 特有的规矩：

- **`max_tokens` 是必填**（官方也是这样）。缺了直接 400 —— 比替它猜一个值更诚实：
  猜小了会把答案截断，而客户端看不到任何提示。
- **流式是有状态的**：不像 chat 那样一 chunk 一转发，必须按
  `message_start → (content_block_start → content_block_delta* → content_block_stop)* →
  message_delta → message_stop` 的顺序发 —— 每个 content block 自己走完一轮才轮到下一个；
  `tool_use` 的 `input_json_delta` 碎片要自己拼。
  （乱序时官方 SDK 并不报错、内容也照样拼得回来，实测 python 1.7 / ts 0.127 都宽容；
  但这不该成为放松顺序的理由，见 `_StreamState` 的说明。）

**想透传思考（thinking/redacted_thinking 块）先读这段**：Anthropic 的 thinking 块要求
服务端用私钥签名，客户端会校验 `signature`。我们给不出合法签名，硬塞一个假块会让客户端报错，
所以这里**既不接收也不回传思考块**：请求里带 `thinking` 块会被忽略并记日志，上游返回的
推理内容（`reasoning_content`）也不译进回包。要思考内容请走 chat 方言（那边是 `reasoning_content`）。
—— 这条和本仓库 README / 官方文档一致，改之前先想清楚签名这一关怎么过。
"""

from __future__ import annotations

import json
import logging

from fastapi.responses import JSONResponse, StreamingResponse

from .responses import _hub_headers, _json_body, _rid, _text_of_parts, _usage_of

logger = logging.getLogger("api-hub")

ANTHROPIC_VERSION = "2023-06-01"

#: chat 的 finish_reason → Anthropic 的 stop_reason（只做这个方向）
_STOP_TO_ANTHROPIC = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def _stop_reason(finish_reason, has_tools: bool) -> str:
    """chat 的 finish_reason → Anthropic 的 stop_reason。优先级是刻意的：

    1. `length`（被 max_tokens 截断）优先 —— 工具参数很可能只写了一半，不能让客户端照着执行
    2. 有 `tool_use` 块 → `tool_use`。**finish_reason 缺失、是 None、被渠道写成 "stop"、
       或者编成一个我们不认识的值，都不该让客户端以为"这一轮正常结束了"** —— 那会让工具
       静默地不被执行，而这在免额渠道上并不罕见。
    3. 其余查表，查不到按 `end_turn` 收。
    """
    if finish_reason == "length":
        return "max_tokens"
    if has_tools:
        return "tool_use"
    return _STOP_TO_ANTHROPIC.get(finish_reason, "end_turn")


#: Anthropic messages 的角色 → chat 角色

#: Anthropic messages 的角色 → chat 角色
_ROLE_MAP = {"user": "user", "assistant": "assistant", "system": "system"}

#: chat 不认识、不能原样往上送的 Anthropic 专属字段
_ANTHROPIC_ONLY = (
    "anthropic_version", "system", "thinking", "metadata", "max_tokens_to_sample",
)


def _text_of_blocks(blocks) -> str:
    """把 Anthropic 的 content（块数组或裸字符串）压成纯文本"""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return "" if blocks is None else str(blocks)
    out = []
    for b in blocks:
        if isinstance(b, str):
            out.append(b)
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            v = b.get("text")
            if isinstance(v, str):
                out.append(v)
        elif t == "tool_result":
            out.extend(_tool_result_text(b.get("content")))
        elif t == "image":
            out.append("[图片]")
        elif t == "document":
            out.append("[文档]")
        elif isinstance(b.get("text"), str):
            out.append(b["text"])
    return "".join(out)


def _tool_result_text(content) -> list:
    """tool_result 的 content：字符串 / 块数组 / 纯文本都接住"""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("[图片]")
        return parts
    if content is None:
        return []
    return [str(content)]


def _image_block_to_chat(b: dict) -> dict | None:
    """Anthropic 的 image 块 → chat 的 image_url 块（data URL 或远端 URL）"""
    src = b.get("source")
    if not isinstance(src, dict):
        return None
    if src.get("type") == "base64" and isinstance(src.get("data"), str):
        media = src.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{src['data']}"}}
    if src.get("type") == "url" and isinstance(src.get("url"), str):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _content_to_chat(blocks, ctx: dict):
    """Anthropic 的 content → chat 的 content。

    纯文本**折叠成一个字符串**（很多免费渠道的兼容层只认字符串形式的 content，
    给块数组会直接 400）；真的带图才保留数组形态。"""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return "" if blocks is None else str(blocks)

    parts = []
    has_image = False
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str):
            parts.append({"type": "text", "text": b["text"]})
        elif t == "image":
            img = _image_block_to_chat(b)
            if img:
                parts.append(img)
                has_image = True
        elif t == "tool_result":
            txt = "".join(_tool_result_text(b.get("content")))
            if txt:
                parts.append({"type": "text", "text": txt})
        elif t in ("thinking", "redacted_thinking"):
            # 见模块 docstring：没有合法签名，收下也会让客户端在下一轮校验失败
            ctx["dropped"].append(t)
        elif isinstance(b.get("text"), str):
            parts.append({"type": "text", "text": b["text"]})

    if not has_image:
        return "".join(p["text"] for p in parts if p.get("type") == "text")
    return parts or ""


def _tool_spec(t: dict) -> dict | None:
    """Anthropic 的 tool（{name, description, input_schema}）→ chat 的 function"""
    if not isinstance(t, dict):
        return None
    name = t.get("name")
    if not isinstance(name, str) or not name:
        return None
    fn = {"name": name}
    if isinstance(t.get("description"), str):
        fn["description"] = t["description"]
    schema = t.get("input_schema")
    fn["parameters"] = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
    return {"type": "function", "function": fn}


def _tools_of(tools, ctx: dict) -> list:
    if not isinstance(tools, list):
        return []
    out = []
    for t in tools:
        spec = _tool_spec(t)
        if spec:
            out.append(spec)
    return out


def _tool_choice_of(tc):
    """Anthropic 的 tool_choice → chat 的 tool_choice

    `any` 只能表达成 `required`（chat 没有"随便挑一个"的等价物，`required` 最接近；
    多余的那个可用工具由客户端自己在 prompt 里约束）。"""
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool" and isinstance(tc.get("name"), str):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None


def _messages_from(body_messages, ctx: dict) -> list:
    """Anthropic 的 messages → chat 的 messages。

    两件必须做的事：

    - **assistant 的 tool_use 块 → `tool_calls`**（`id` 原样沿用：多轮工具调用全靠它对上，
      客户端下一轮发的 `tool_result.tool_use_id` 就是这个 id）
    - **user 的 tool_result 块 → `role:"tool"` 消息**（`tool_use_id` → `tool_call_id`）。
      **不替客户端补 assistant 占位消息**：真实的 `tool_use` 就在上文那条 assistant 消息里，
      伪造一条反而会污染上下文（旧写法在这里自己踩过）。
    """
    out = []
    for m in body_messages:
        if not isinstance(m, dict):
            continue
        role = _ROLE_MAP.get(m.get("role"), "user")
        content = m.get("content")

        if role == "assistant" and isinstance(content, list):
            text_parts, tool_calls = [], []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    text_parts.append(b["text"])
                elif b.get("type") == "tool_use":
                    args = b.get("input")
                    tool_calls.append({
                        "id": b.get("id") if isinstance(b.get("id"), str) else _rid("call_"),
                        "type": "function",
                        "function": {
                            "name": str(b.get("name") or ""),
                            "arguments": json.dumps(args if isinstance(args, (dict, list)) else {},
                                                     ensure_ascii=False),
                        },
                    })
                elif b.get("type") in ("thinking", "redacted_thinking"):
                    ctx["dropped"].append(b["type"])
            msg = {"role": "assistant", "content": "".join(text_parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
                if not text_parts:
                    msg["content"] = None
            out.append(msg)
            continue

        if role == "user" and isinstance(content, list):
            # tool_result 和普通文本要分开：前者必须是独立的 role:"tool" 消息
            texts, results = [], []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    results.append(b)
                else:
                    texts.append(b)
            if texts:
                out.append({"role": "user", "content": _content_to_chat(texts, ctx)})
            for r in results:
                out.append({
                    "role": "tool",
                    "tool_call_id": r.get("tool_use_id") if isinstance(r.get("tool_use_id"), str)
                    else _rid("call_"),
                    "content": "".join(_tool_result_text(r.get("content"))),
                })
            continue

        out.append({"role": role, "content": _content_to_chat(content, ctx)})
    return out


def to_chat(body: dict, cfg: dict | None = None) -> tuple[dict, dict]:
    """Anthropic 请求体 → `(chat 请求体, 上下文)`；客户端写错了抛 ValueError（→ 400）"""
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("缺少 model 字段")
    model = model.strip()

    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise ValueError("messages 不能为空：Anthropic 协议要求把完整对话放在这里（网关无状态）")

    # `max_tokens` 必填（官方也这么要求）。不替它猜：猜小了会把答案截断，而客户端看不到提示。
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("缺少 max_tokens（Anthropic 协议必填，正整数）")

    ctx = {
        "model": model,
        "dropped": [],
        "system": body.get("system"),
        "tools": body.get("tools") if isinstance(body.get("tools"), list) else [],
        "tool_choice": body.get("tool_choice"),
        "thinking": body.get("thinking") if isinstance(body.get("thinking"), dict) else None,
    }

    chat: dict = {"model": model, "max_tokens": max_tokens}

    system_text = _text_of_blocks(body.get("system"))
    messages = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.extend(_messages_from(msgs, ctx))
    chat["messages"] = messages

    tools = _tools_of(body.get("tools"), ctx)
    if tools:
        chat["tools"] = tools
        choice = _tool_choice_of(body.get("tool_choice"))
        if choice:
            chat["tool_choice"] = choice

    # 与 chat / responses 入口一致的常见参数，名字对得上就原样搬
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("stop_sequences", "stop"), ("top_k", "top_k")):
        v = body.get(src)
        if v is not None:
            chat[dst] = v

    if body.get("stream"):
        chat["stream"] = True
        so = body.get("stream_options")
        if isinstance(so, dict):
            chat["stream_options"] = so
        elif (cfg or {}).get("responses_stream_usage", True):
            # 和 responses 方言同一个开关：流式回包的 usage 靠它。
            # 哪个渠道因此报 400 就在 config 里关掉（responses_stream_usage=false）。
            chat["stream_options"] = {"include_usage": True}

    if ctx["thinking"]:
        ctx["dropped"].append("thinking")
    if ctx["dropped"]:
        logger.info("messages 翻译: model=%s 丢弃=%s", model, sorted(set(ctx["dropped"])))
    return chat, ctx


# ---------------------------------------------------------------- 响应翻译

def _tool_use_blocks(msg: dict) -> list:
    """chat 的 tool_calls → Anthropic 的 tool_use 块（id 原样沿用）"""
    out = []
    for tc in (msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {"input": args}
        out.append({"type": "tool_use",
                    "id": tc.get("id") if isinstance(tc.get("id"), str) else _rid("toolu_"),
                    "name": str(fn.get("name") or ""),
                    "input": args})
    return out


def build_message(data: dict, ctx: dict) -> dict:
    """chat 的响应体 → Anthropic 的 message 对象"""
    data = data if isinstance(data, dict) else {}
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    ch = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}

    blocks = []
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(_tool_use_blocks(msg))
    if not blocks:
        # 空回复也要给一个块：Anthropic 的 content 不允许为空数组（客户端会当协议错误）
        blocks.append({"type": "text", "text": ""})

    stop = _stop_reason(ch.get("finish_reason"), bool(msg.get("tool_calls")))

    usage = _usage_of(data.get("usage"))
    return {
        "id": data.get("id") if isinstance(data.get("id"), str) and data.get("id") else _rid("msg_"),
        "type": "message",
        "role": "assistant",
        "model": ctx.get("model"),
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]},
    }


def _sse(event: str, payload: dict) -> bytes:
    """Anthropic 的 SSE 帧：**必须带 event: 行**（官方 SDK 按事件名分派）"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _chat_delta(line: bytes) -> dict:
    """解析一行 chat SSE（`data: {...}`），返回增量字典；不是数据行就返回空 dict"""
    line = line.strip()
    if not line.startswith(b"data:"):
        return {}
    payload = line[5:].strip()
    if payload in (b"[DONE]", b"[done]"):
        return {"done": True}
    try:
        j = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(j, dict):
        return {}
    out: dict = {}
    if isinstance(j.get("usage"), dict) and j["usage"]:
        out["usage"] = j["usage"]
    if isinstance(j.get("model"), str) and j["model"]:
        out["model"] = j["model"]
    choices = j.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if not isinstance(c, dict):
                continue
            delta = c.get("delta") if isinstance(c.get("delta"), dict) else \
                (c.get("message") if isinstance(c.get("message"), dict) else {})
            for key in ("reasoning_content", "reasoning"):
                v = delta.get(key)
                if isinstance(v, str) and v:
                    out["reasoning"] = out.get("reasoning", "") + v
            content = delta.get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if isinstance(content, str) and content:
                out["text"] = out.get("text", "") + content
            tcs = delta.get("tool_calls")
            if isinstance(tcs, list):
                out.setdefault("tool_calls", []).extend([t for t in tcs if isinstance(t, dict)])
            if c.get("finish_reason"):
                out["finish_reason"] = c["finish_reason"]
    return out


class _StreamState:
    """chat SSE → Anthropic 事件流的逐请求状态机。

    顺序按官方规定来：`message_start` 在最前，每个 content block 自己 `start → delta* →
    stop` 走完才开下一个，所有 block 都关完才发 `message_delta` / `message_stop`。
    （乱序时官方 SDK 其实不报错、内容也拼得回来，所以这不是"会不会崩"的问题，而是
    "要不要守约"的问题 —— 按规范发，客户端的宽容度就不必被消耗在这种地方。）

    - 文本块与工具块是分开的 content block（chat 里它们混在同一个 delta 里）
    - `input_json_delta` 负责把工具参数**分片**发出去，客户端自己拼回 JSON
    - 思考（`reasoning_content`）不译进回包，见模块 docstring
    """

    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.msg_id = _rid("msg_")
        self.model = ctx.get("model")
        self.block_idx = -1          # 已经开过几个 content block
        self.open_idx = None         # 当前**打开着**的 block；同一时刻只能有一个
        self.open_kind = None        # "text" / "tool"
        self.open_key = None         # 工具块对应的 chat tool_call index
        self.text_buf = ""
        self.tools: dict = {}        # chat tool_call index → {"idx", "id", "name", "args"}
        self.usage: dict = {}
        self.finish_reason = None
        self.buf = b""

    def start(self) -> list:
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": self.msg_id, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }})]

    # ---- block 生命周期 ----
    # Anthropic 要求**每个 block 自己 start → delta* → stop 走完**，上一个还没 stop 就开
    # 下一个属于乱序。所以这里统一用「开新块之前先关掉旧块」，顺序就不会散。
    def _close_open(self) -> list:
        if self.open_idx is None:
            return []
        idx = self.open_idx
        self.open_idx = self.open_kind = self.open_key = None
        return [_sse("content_block_stop", {"type": "content_block_stop", "index": idx})]

    def _open_block(self, kind: str, content_block: dict, key=None) -> list:
        """关掉当前块，开一个新块（index 递增），并把它记成「打开着」"""
        evts = self._close_open()
        self.block_idx += 1
        self.open_idx, self.open_kind, self.open_key = self.block_idx, kind, key
        evts.append(_sse("content_block_start", {
            "type": "content_block_start", "index": self.block_idx,
            "content_block": content_block,
        }))
        return evts

    # ---- 文本块 ----
    def _text(self, s: str) -> list:
        # 当前开着的就是文本块 → 接着写；否则（没开 / 开着的是工具块）另起一个
        evts = [] if self.open_kind == "text" else \
            self._open_block("text", {"type": "text", "text": ""})
        self.text_buf += s
        evts.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": self.open_idx,
            "delta": {"type": "text_delta", "text": s},
        }))
        return evts

    # ---- 工具块 ----
    def _tool(self, tc: dict) -> list:
        """一个 tool_call 增量 → 事件。

        同一个 `index` 的连续增量写进**同一个** content block（这就是 `input_json_delta`
        分片的来处）；换到别的 `index` 时先把当前块关掉再开新块 —— 并行工具调用因此被拆成
        「块 N 整段走完 → 块 N+1 整段」，顺序才合法。

        上游若把不同 index 的参数分片**交错**送过来（先 announce 多个 index，之后轮流补
        arguments），Anthropic 的块模型表达不了，只能等流结束再发；这里退一步处理：为那个
        index 另开一块继续写，并记一条 warning。实际上模型是按顺序生成各工具参数的 JSON，
        不会交错，这条分支是防御性的。
        """
        key = tc.get("index")
        if not isinstance(key, int) or isinstance(key, bool):
            key = 0
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name")
        evts: list = []
        st = self.tools.get(key)

        if st is None:
            st = {"idx": None,
                  "id": tc.get("id") if isinstance(tc.get("id"), str) and tc.get("id")
                  else _rid("toolu_"),
                  "name": name if isinstance(name, str) else "",
                  "args": ""}
            self.tools[key] = st
        elif isinstance(name, str) and name and not st["name"]:
            st["name"] = name

        if self.open_kind != "tool" or self.open_key != key:
            if st["idx"] is not None:
                logger.warning("messages: 上游把 tool_call[%s] 的分片与别的块交错发来"
                               "（Anthropic 的块模型表达不了，已另开一块继续写）", key)
            evts += self._open_block("tool", {
                "type": "tool_use", "id": st["id"], "name": st["name"], "input": {},
            }, key=key)
            st["idx"] = self.open_idx

        args = fn.get("arguments")
        if isinstance(args, str) and args:
            st["args"] += args
            evts.append(_sse("content_block_delta", {
                "type": "content_block_delta", "index": self.open_idx,
                "delta": {"type": "input_json_delta", "partial_json": args},
            }))
        return evts

    # ---- 喂数据 ----
    def feed(self, chunk: bytes) -> list:
        self.buf += chunk
        evts: list = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            evts += self._line(line)
        return evts

    def _line(self, line: bytes) -> list:
        d = _chat_delta(line)
        if not d:
            return []
        if isinstance(d.get("usage"), dict):
            self.usage = d["usage"]
        if d.get("model"):
            self.model = d["model"]
        evts: list = []
        if d.get("text"):
            evts += self._text(d["text"])
        for tc in (d.get("tool_calls") or []):
            evts += self._tool(tc)
        if d.get("finish_reason"):
            self.finish_reason = d["finish_reason"]
        return evts

    # ---- 收尾 ----
    def finish(self, err: str | None = None) -> list:
        evts = self._close_open()
        if err:
            # 上游中途断了：按 Anthropic 的错误事件收尾（客户端认这个）
            evts.append(_sse("error", {"type": "error",
                                       "error": {"type": "api_error", "message": err[:400]}}))
            return evts

        stop = _stop_reason(self.finish_reason, bool(self.tools))
        usage = _usage_of(self.usage)
        evts.append(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"]},
        }))
        evts.append(_sse("message_stop", {"type": "message_stop"}))
        return evts


async def translate_stream(inner, ctx: dict):
    """chat 的 SSE 字节流 → Anthropic 的事件流。

    `inner` 就是 `main._stream_gen` 那个生成器（`_chat_v1` 把它包进 StreamingResponse）。
    它自己已经在做记账（usage 抽取、成败判定、TTFT），这里只**旁路解析**同一条字节流，
    不改动它看到的任何东西 —— 客户端断开时 GeneratorExit 原样穿透回去。
    """
    st = _StreamState(ctx)
    for e in st.start():
        yield e
    err = None
    try:
        async for chunk in inner:
            if not chunk:
                continue
            for e in st.feed(chunk):
                yield e
    except Exception as e:            # noqa: BLE001 —— 上游断流不该把异常甩给客户端
        err = f"{type(e).__name__}: {e}"
        logger.warning("messages: 上游流中断，回 error 事件: %s", err[:180])
    for e in st.finish(err):
        yield e


def error_response(status: int, message: str) -> JSONResponse:
    """Anthropic 的错误体形状：`{"type":"error","error":{"type":...,"message":...}}`"""
    if status >= 500:
        typ = "api_error"
    elif status == 404:
        typ = "not_found_error"
    elif status == 429:
        typ = "rate_limit_error"
    elif status == 401:
        typ = "authentication_error"
    elif status == 403:
        typ = "permission_error"
    else:
        typ = "invalid_request_error"
    return JSONResponse({"type": "error", "error": {"type": typ, "message": message}},
                        status_code=status)


async def convert(resp, ctx: dict):
    """`_chat_v1` 的返回值 → Anthropic 协议的返回值（流式/非流式同一个入口）"""
    headers = _hub_headers(resp)
    if isinstance(resp, StreamingResponse):
        headers["Cache-Control"] = "no-cache"
        headers["anthropic-version"] = ANTHROPIC_VERSION
        return StreamingResponse(translate_stream(resp.body_iterator, ctx),
                                 media_type="text/event-stream", headers=headers)
    if getattr(resp, "status_code", 200) >= 400:
        body = _json_body(resp)
        msg = body.get("detail") if isinstance(body.get("detail"), str) else "上游渠道均失败"
        return error_response(int(resp.status_code), msg)
    out = JSONResponse(build_message(_json_body(resp), ctx), headers=headers)
    out.headers["anthropic-version"] = ANTHROPIC_VERSION
    return out


__all__ = ["to_chat", "convert", "translate_stream", "build_message", "error_response",
           "ANTHROPIC_VERSION"]
