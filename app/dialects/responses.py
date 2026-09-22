"""OpenAI Responses API（`/v1/responses`）→ Chat Completions 翻译层

对外：让只认 Responses 协议的客户端（Codex CLI、新版 OpenAI SDK、部分 Agent 框架）
能用上这个网关；对内：把请求翻译成 `/v1/chat/completions` 的请求体，把上游的
chat 响应翻译回 Responses 的响应对象 / SSE 事件流。

**路由不在这里**：翻译完的请求交给 `main._chat_v1`，渠道选择、冷却分类、failover、
会话粘性、用量记账全部沿用 chat 入口那一套 —— 这里只做协议映射。

三条设计约束：

1. **无状态**。不实现 `store` 的服务端记忆，也不认 `previous_response_id`
   （客户端每轮把完整历史放在 `input` 里，Responses 协议本身就是这么用的）。
   收到 `previous_response_id` 会记一条 warning 并忽略。

2. **失败不致命**。上游多返回一个字段、少返回一个字段、`delta` 用什么奇怪写法，
   都不该让整条请求崩掉。所有解析都带类型检查，认不出来的部分降级丢弃。
   只有「客户端侧写错了」才抛 `ValueError`（→ HTTP 400）：缺 model、input 类型不对、
   input 为空。

3. **不做字段清洗**。与 chat 协议不冲突的字段一律原样透传，口径与 chat 入口一致。
   两个例外（都是「加了可能让整条请求 400」的字段）由配置开关控制，见 `to_chat`。

—— 工具调用（function calling）的往返是这里最要紧的一条链路：

    请求 `function_call`         → assistant.tool_calls[{id: **call_id**, ...}]
    请求 `function_call_output`  → {"role": "tool", "tool_call_id": **call_id**}
    响应 tool_calls              → output[].type == "function_call"，call_id 沿用上游 id

  只要 `call_id` 不变，多轮工具调用就能闭合。**改这段前先看
  `tests/test_responses.py::test_tool_call_round_trip_preserves_call_id`。**
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("api-hub")


# ---------------------------------------------------------------- 请求翻译

# Responses → chat 同义字段，原样搬运
_PASSTHROUGH_FIELDS = (
    "temperature", "top_p", "seed", "user", "stop",
    "presence_penalty", "frequency_penalty", "logit_bias", "logprobs", "top_logprobs",
    "parallel_tool_calls", "max_completion_tokens", "response_format", "service_tier",
)

# Responses 的消息角色 → chat 的消息角色（developer 就是新版 system）
_ROLE_MAP = {"developer": "system", "system": "system", "user": "user",
             "assistant": "assistant", "tool": "tool"}

# 没有 chat 对应物、直接丢掉并记账的 input item 类型
_SKIP_ITEM_TYPES = {"item_reference", "reasoning", "compaction"}

_MAX_INT = 2 ** 31 - 1


def _rid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _as_text(v) -> str:
    """把任意 input/output 负载压成纯文本（压不动就 JSON 化，绝不返回 None）"""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _text_of_parts(parts) -> str:
    """从 Responses 的内容片段数组里抠出纯文本（图片/文件只留占位说明）"""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return _as_text(parts)
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
            continue
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "summary_text"):
            v = p.get("text")
            if isinstance(v, str):
                out.append(v)
        elif t == "refusal":
            v = p.get("refusal")
            if isinstance(v, str):
                out.append(v)
        elif t == "input_image":
            out.append("[图片]" if (p.get("image_url") or p.get("file_id")) else "[图片]")
        elif t == "input_file":
            out.append(f"[文件 {p.get('filename') or p.get('file_id') or ''}]".strip())
        elif isinstance(p.get("text"), str):
            out.append(p["text"])
    return "".join(out)


def _chat_content(content):
    """Responses 的 content → chat 的 content。

    纯文本会被**折叠成一个字符串**：很多免费渠道的兼容层只认字符串形式的 content，
    给它片段数组会直接 400；只有真的带图才保留数组形态。"""
    if isinstance(content, str) or content is None:
        return content if isinstance(content, str) else ""
    if not isinstance(content, list):
        return _as_text(content)
    parts, has_image = [], False
    for p in content:
        if isinstance(p, str):
            parts.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "summary_text"):
            v = p.get("text")
            parts.append({"type": "text", "text": v if isinstance(v, str) else ""})
        elif t == "refusal":
            parts.append({"type": "text", "text": _as_text(p.get("refusal"))})
        elif t == "input_image":
            url = p.get("image_url")
            if isinstance(url, dict):            # 少数客户端把 image_url 写成对象
                url = url.get("url")
            if isinstance(url, str) and url:
                iu = {"url": url}
                if p.get("detail"):
                    iu["detail"] = p["detail"]
                parts.append({"type": "image_url", "image_url": iu})
                has_image = True
            else:
                # 网关没有文件存储，file_id 解不出来 → 留占位，别把整条消息丢掉
                parts.append({"type": "text",
                              "text": f"[图片 file_id={p.get('file_id') or '?'}]"})
        elif t == "input_file":
            nm = p.get("filename") or p.get("file_id") or ""
            parts.append({"type": "text", "text": f"[文件 {nm}]".strip()})
        elif isinstance(p.get("text"), str):
            parts.append({"type": "text", "text": p["text"]})
    if not has_image:
        return "".join(x["text"] for x in parts)
    return parts or ""


def _append_tool_call(messages: list, item: dict):
    """function_call item → assistant.tool_calls。

    连续的 function_call 合并进**同一条** assistant 消息（chat 协议就是这么表达的：
    一条 assistant 消息挂多个 tool_calls，后面跟多条 tool 结果）。"""
    call_id = item.get("call_id") or item.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = _rid("call_")
    fn = {"name": item.get("name") if isinstance(item.get("name"), str) else "",
          "arguments": item.get("arguments") if isinstance(item.get("arguments"), str)
          else _as_text(item.get("arguments") or "{}")}
    last = messages[-1] if messages else None
    if (isinstance(last, dict) and last.get("role") == "assistant"
            and last.get("tool_calls") and not last.get("content")):
        last["tool_calls"].append({"id": call_id, "type": "function", "function": fn})
        return
    messages.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": call_id, "type": "function", "function": fn}]})


def _messages_from_input(inp, messages: list, ctx: dict):
    if inp is None:
        return
    if isinstance(inp, str):
        if inp:
            messages.append({"role": "user", "content": inp})
        return
    if not isinstance(inp, list):
        raise ValueError("input 必须是字符串或数组")

    for item in inp:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "function_call":
            _append_tool_call(messages, item)
            continue
        if t == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") if isinstance(item.get("call_id"), str)
                else _rid("call_"),
                "content": _text_of_parts(item.get("output")),
            })
            continue
        if isinstance(t, str) and t in _SKIP_ITEM_TYPES:
            ctx["dropped"].append(t)
            continue
        role = item.get("role")
        if isinstance(role, str) and role:
            messages.append({"role": _ROLE_MAP.get(role, "user"),
                             "content": _chat_content(item.get("content"))})
            continue
        if isinstance(t, str) and t.endswith("_call_output"):
            # computer_call_output / local_shell_call_output 之类：当一条 user 文本塞回去，
            # 总比整条丢掉强（模型至少能看到「这一步的产出」）
            messages.append({"role": "user", "content": _text_of_parts(item.get("output"))})
            continue
        ctx["dropped"].append(t or "?")


def _tool_spec(fn: dict) -> dict:
    spec = {
        "name": fn.get("name") or "",
        "description": fn.get("description") or "",
        "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict)
        else {"type": "object", "properties": {}},
    }
    if fn.get("strict") is not None:
        spec["strict"] = bool(fn["strict"])
    return spec


def _tools_of(tools, ctx: dict) -> list:
    """Responses 的 tools → chat 的 tools。

    - `{"type":"function", name, parameters}`   → `{"type":"function","function":{...}}`
    - `{"type":"custom", name, format:{grammar}}` → 包成单参数 function（chat 没有
      custom tool 概念），参数名固定 `input`；响应侧会原样还原成 `custom_tool_call`
      （见 `_StreamState._tool_item` / `build_response`）。
    - 内置托管工具（web_search_preview / file_search / computer_use_preview …）网关
      服务不了 → 丢掉并记账，**不会**因为客户端声明了它们而让整条请求失败。
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        typ = t.get("type")
        if typ == "function" or (typ is None and "function" in t):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            spec = _tool_spec(fn)
            if not spec["name"]:
                continue
            out.append({"type": "function", "function": spec})
        elif typ == "custom":
            name = (t.get("name") or "").strip()
            if not name:
                continue
            desc = (t.get("description") or "").strip()
            out.append({"type": "function", "function": {
                "name": name,
                "description": (desc + " " if desc else "")
                + "（参数放在 input 字段，值为字符串原文）",
                "parameters": {"type": "object", "properties": {
                    "input": {"type": "string"}}, "required": ["input"]},
            }})
            ctx["custom_tools"].add(name)
        elif typ == "namespace":
            # Codex 0.15x 把 MCP 工具打包成 {"type":"namespace", tools:[...]}。
            # chat 协议没有命名空间概念 → 摊平成普通 function，记下名字↔命名空间，
            # 回包时给 function_call 补 namespace 字段（codex 靠它路由到 MCP server）。
            ns = (t.get("name") or "").strip()
            for spec in _tools_of(t.get("tools"), ctx):
                out.append(spec)
                if ns and isinstance(spec.get("function"), dict):
                    ctx["ns_of"][spec["function"]["name"]] = ns
        else:
            ctx["dropped"].append(typ or "?")
    return out


def _tool_choice_of(tc) -> str | dict | None:
    if isinstance(tc, str):
        if tc in ("auto", "none", "required"):
            return tc
        return "auto"
    if isinstance(tc, dict):
        t = tc.get("type")
        name = tc.get("name")
        if not isinstance(name, str) or not name:
            f = tc.get("function")
            name = f.get("name") if isinstance(f, dict) else None
        if t in ("function", "custom") and isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
        if t == "allowed_tools":
            return "required" if tc.get("mode") == "required" else "auto"
        if t == "none":
            return "none"
    return None


def _response_format_of(text) -> dict | None:
    """Responses 的 `text.format` → chat 的 `response_format`"""
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    t = fmt.get("type")
    if t == "json_schema":
        src = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else fmt
        sch = {"name": src.get("name") or "response",
               "schema": src.get("schema") if isinstance(src.get("schema"), dict) else {}}
        if src.get("strict") is not None:
            sch["strict"] = bool(src["strict"])
        return {"type": "json_schema", "json_schema": sch}
    if t == "json_object":
        return {"type": "json_object"}
    return None


def to_chat(body: dict, cfg: dict | None = None) -> tuple[dict, dict]:
    """Responses 请求体 → `(chat 请求体, 上下文)`；客户端写错了抛 ValueError（→ 400）。

    `ctx` 存的是「回包时要原样echo回去」的字段，以及两个集合：
    `custom_tools`（哪些工具名要用 custom_tool_call 还原）、`dropped`（记了日志的丢弃项）。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("缺少 model 字段")
    model = model.strip()
    cfg = cfg or {}
    ins = body.get("instructions")          # 官方允许字符串或内容片段数组两种形态
    if not isinstance(ins, str):
        ins = _text_of_parts(ins) if isinstance(ins, list) else None

    ctx = {"model": model, "custom_tools": set(), "dropped": [], "ns_of": {},
           "instructions": ins or None,
           "tools": body.get("tools") if isinstance(body.get("tools"), list) else [],
           "tool_choice": body.get("tool_choice"),
           "parallel_tool_calls": body.get("parallel_tool_calls", True),
           "store": bool(body.get("store", False)),
           "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
           "user": body.get("user"),
           "reasoning": body.get("reasoning") if isinstance(body.get("reasoning"), dict) else None,
           "text": body.get("text") if isinstance(body.get("text"), dict) else {"format": {"type": "text"}},
           "truncation": body.get("truncation") or "disabled",
           "temperature": body.get("temperature"),
           "top_p": body.get("top_p"),
           "max_output_tokens": body.get("max_output_tokens"),
           }

    if body.get("previous_response_id"):
        # 网关无状态：没有服务端会话可续。客户端自己回传全量 input 就不受影响。
        logger.warning("responses: 客户端传了 previous_response_id=%r，网关无状态、已忽略"
                       "（请把完整对话放进 input）", str(body["previous_response_id"])[:60])

    messages = []
    if ctx["instructions"]:
        messages.append({"role": "system", "content": ctx["instructions"]})
    _messages_from_input(body.get("input"), messages, ctx)
    if not messages:
        raise ValueError("input 为空：至少需要一条消息（网关无状态，不支持只靠 previous_response_id 续话）")

    chat = {"model": model, "messages": messages}

    tools = _tools_of(body.get("tools"), ctx)
    if tools:
        chat["tools"] = tools
        choice = _tool_choice_of(body.get("tool_choice"))
        if choice:
            chat["tool_choice"] = choice

    mot = body.get("max_output_tokens")
    if isinstance(mot, int) and not isinstance(mot, bool) and 0 < mot <= _MAX_INT:
        chat["max_tokens"] = mot

    rf = _response_format_of(body.get("text"))
    if rf:
        chat["response_format"] = rf

    for k in _PASSTHROUGH_FIELDS:
        if k in body and body[k] is not None:
            chat[k] = body[k]

    # reasoning.effort 默认**不**透传：个别渠道不认 `reasoning_effort` 会整条 400，
    # 而少了它只是丢掉一个提示、模型仍按自己的默认档位跑。需要就开
    # config 的 responses_reasoning_effort。
    eff = (body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else None
    if isinstance(eff, str) and eff in ("minimal", "low", "medium", "high"):
        if cfg.get("responses_reasoning_effort", False):
            chat["reasoning_effort"] = eff
        else:
            ctx["dropped"].append(f"reasoning.effort={eff}(未开启透传)")

    is_stream = bool(body.get("stream"))
    if is_stream:
        chat["stream"] = True
        so = body.get("stream_options")
        if isinstance(so, dict):
            chat["stream_options"] = so
        elif cfg.get("responses_stream_usage", True):
            # 上游不给 usage 块时，流式回包的 response.completed 里就没有 token 用量。
            # 主流兼容层都认这个字段；哪个渠道因此 400 就把开关关掉。
            chat["stream_options"] = {"include_usage": True}

    if ctx["dropped"] or ctx["custom_tools"]:
        logger.info("responses 翻译: model=%s 丢弃=%s%s",
                    model, sorted(set(ctx["dropped"])) or "-",
                    f" custom工具={sorted(ctx['custom_tools'])}" if ctx["custom_tools"] else "")
    return chat, ctx


# ---------------------------------------------------------------- 响应翻译

def _reasoning_text(d: dict) -> str:
    """从 delta / message 里抠出推理正文（各家叫法不一）"""
    for k in ("reasoning_content", "reasoning"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _usage_of(u) -> dict:
    u = u if isinstance(u, dict) else {}
    pt = u.get("prompt_tokens") if isinstance(u.get("prompt_tokens"), int) else 0
    ct = u.get("completion_tokens") if isinstance(u.get("completion_tokens"), int) else 0
    tt = u.get("total_tokens") if isinstance(u.get("total_tokens"), int) else pt + ct
    pd = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    cd = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
    return {
        "input_tokens": pt,
        "input_tokens_details": {"cached_tokens": pd.get("cached_tokens") or 0},
        "output_tokens": ct,
        "output_tokens_details": {"reasoning_tokens": cd.get("reasoning_tokens") or 0},
        "total_tokens": tt,
    }


def _unwrap_custom(args: str) -> str:
    """custom tool 的参数在 chat 里被包成 `{"input": "..."}`（见 `_tools_of`），这里还原成裸文本。
    解不出来就原样返回 —— 宁可让客户端拿到一段 JSON，也不能让 item 变空。"""
    if not args:
        return ""
    try:
        d = json.loads(args)
    except (TypeError, ValueError):
        return args
    if isinstance(d, dict) and "input" in d:
        v = d["input"]
        return v if isinstance(v, str) else _as_text(v)
    return args


def _fn_item(name: str, call_id: str, args: str, custom: bool, status: str = "completed",
             namespace: str | None = None) -> dict:
    """function tool → function_call item；custom tool → custom_tool_call item

    `namespace`：请求侧摊平过 namespace 工具时补回原命名空间名（codex 用它把
    function_call 路由回对应 MCP server；官方 Responses 的 function_call 带此字段）。"""
    if custom:
        return {"id": _rid("ctc_"), "type": "custom_tool_call", "call_id": call_id,
                "name": name, "input": _unwrap_custom(args), "status": status}
    item = {"id": _rid("fc_"), "type": "function_call", "call_id": call_id,
            "name": name, "arguments": args, "status": status}
    if namespace:
        item["namespace"] = namespace
    return item


def _output_items(msg: dict, custom: set, ns_of: dict | None = None) -> tuple[list, str]:
    """chat 的 assistant message → Responses 的 output 数组 + status"""
    out = []
    rc = _reasoning_text(msg)
    if rc:
        out.append({"id": _rid("rs_"), "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": rc}]})
    content = msg.get("content")
    if isinstance(content, list):
        content = _text_of_parts(content)
    if isinstance(content, str) and content:
        out.append({"id": _rid("msg_"), "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}]})
    if msg.get("refusal"):
        out.append({"id": _rid("msg_"), "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": _as_text(msg["refusal"])}]})
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name") if isinstance(fn.get("name"), str) else ""
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = _as_text(args if args is not None else {})
        call_id = tc.get("id") if isinstance(tc.get("id"), str) and tc.get("id") else _rid("call_")
        out.append(_fn_item(name, call_id, args, name in custom,
                            namespace=(ns_of or {}).get(name)))
    return out


def build_response(data: dict, ctx: dict) -> dict:
    """chat 的完整响应 JSON → Responses 的响应对象"""
    data = data if isinstance(data, dict) else {}
    choices = data.get("choices")
    ch0 = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    msg = ch0.get("message") if isinstance(ch0.get("message"), dict) else {}
    finish = ch0.get("finish_reason")
    err = data.get("error")

    if err:
        status, error = "failed", {
            "code": (err.get("code") or err.get("type") or "upstream_error")
            if isinstance(err, dict) else "upstream_error",
            "message": _as_text(err.get("message") if isinstance(err, dict) else err)[:800],
        }
        out = []
        usage = None
    else:
        out = _output_items(msg, set(ctx.get("custom_tools") or ()), ctx.get("ns_of"))
        status = "incomplete" if finish in ("length", "content_filter") else "completed"
        error = None
        usage = _usage_of(data.get("usage"))
        if not ch0:
            logger.warning("responses: 上游 200 但没有 choices，回空 output（model=%s）",
                           ctx.get("model"))

    if not out and status == "completed" and not err:
        logger.warning("responses: 模型没产出任何内容（model=%s finish=%s）",
                       ctx.get("model"), finish)

    return {
        "id": _rid("resp_"),
        "object": "response",
        "created_at": int(data.get("created") or time.time()),
        "status": status,
        "background": False,
        "error": error,
        "incomplete_details": ({"reason": "max_output_tokens"} if finish == "length"
                              else {"reason": "content_filter"} if finish == "content_filter"
                              else None),
        "instructions": ctx.get("instructions"),
        "max_output_tokens": ctx.get("max_output_tokens")
        if isinstance(ctx.get("max_output_tokens"), int) else None,
        "max_tool_calls": None,
        "model": data.get("model") or ctx.get("model"),
        "output": out,
        "parallel_tool_calls": bool(ctx.get("parallel_tool_calls", True)),
        "previous_response_id": None,
        "prompt_cache_key": None,
        "reasoning": ctx.get("reasoning"),
        "safety_identifier": None,
        "service_tier": "default",
        "store": bool(ctx.get("store", False)),
        "temperature": ctx.get("temperature"),
        "text": ctx.get("text") or {"format": {"type": "text"}},
        "tool_choice": ctx.get("tool_choice") if ctx.get("tool_choice") is not None else "auto",
        "tools": ctx.get("tools") or [],
        "top_logprobs": 0,
        "top_p": ctx.get("top_p"),
        "truncation": ctx.get("truncation") or "disabled",
        "usage": usage,
        "user": ctx.get("user"),
        "metadata": ctx.get("metadata") or {},
    }


# ---------------------------------------------------------------- 流式翻译

def _sse(typ: str, payload: dict) -> bytes:
    """Responses 的 SSE 帧：`event:` 与 `data:` 都带（两派客户端各读一个）"""
    return f"event: {typ}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _StreamState:
    """chat SSE → Responses 事件流的逐请求状态机。

    output item 是**按需打开**的：只有真的来了 content / reasoning / tool_call 增量才
    开一个 item，并在类型切换或流结束时关掉（发 `*.done` + `output_item.done`）。
    这样纯工具调用的应答不会凭空多出一个空的 message item —— 与官方行为一致。

    事件名与字段照官方抄，客户端不认识的类型会自行忽略；发出去的 `call_id` 必须与
    上游 tool_call 的 id 完全一致，否则下一轮 `function_call_output` 对不上。
    """

    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.resp_id = _rid("resp_")
        self.created = int(time.time())
        self.model = ctx.get("model")
        self.custom = set(ctx.get("custom_tools") or ())
        self.ns_of = ctx.get("ns_of") or {}   # function 名 → 原 namespace 名（摊平回来的）
        self.seq = 0
        self.out_idx = 0
        self.items = []          # 已关闭的 output item（喂给 response.completed）
        self.cur = None          # 当前打开的 item（dict）
        self.tools = {}          # chat tool_call index → item 状态
        self.usage = {}
        # ⚠️ 必须叫 finish_reason 而不是 finish —— 后者会被同名方法 `finish()` 覆盖成
        # NoneType，收尾时报 "'NoneType' object is not callable"（写错一次，记在这）。
        self.finish_reason = None
        self.buf = b""

    # ---- 基础 ----
    def ev(self, typ: str, **payload) -> bytes:
        body = {"type": typ, "sequence_number": self.seq}
        body.update(payload)
        self.seq += 1
        return _sse(typ, body)

    def envelope(self, status: str, error=None) -> dict:
        return {
            "id": self.resp_id, "object": "response", "created_at": self.created,
            "status": status, "background": False, "error": error,
            "incomplete_details": ({"reason": "max_output_tokens"} if self.finish_reason == "length"
                                  else {"reason": "content_filter"} if self.finish_reason == "content_filter"
                                  else None),
            "instructions": self.ctx.get("instructions"),
            "max_output_tokens": self.ctx.get("max_output_tokens")
            if isinstance(self.ctx.get("max_output_tokens"), int) else None,
            "max_tool_calls": None,
            "model": self.model,
            "output": list(self.items),
            "parallel_tool_calls": bool(self.ctx.get("parallel_tool_calls", True)),
            "previous_response_id": None,
            "prompt_cache_key": None,
            "reasoning": self.ctx.get("reasoning"),
            "safety_identifier": None,
            "service_tier": "default",
            "store": bool(self.ctx.get("store", False)),
            "temperature": self.ctx.get("temperature"),
            "text": self.ctx.get("text") or {"format": {"type": "text"}},
            "tool_choice": self.ctx.get("tool_choice") if self.ctx.get("tool_choice") is not None else "auto",
            "tools": self.ctx.get("tools") or [],
            "top_logprobs": 0,
            "top_p": self.ctx.get("top_p"),
            "truncation": self.ctx.get("truncation") or "disabled",
            "usage": _usage_of(self.usage) if self.usage else None,
            "user": self.ctx.get("user"),
            "metadata": self.ctx.get("metadata") or {},
        }

    def start(self) -> list:
        env = self.envelope("in_progress")
        return [self.ev("response.created", response=env),
                self.ev("response.in_progress", response=env)]

    # ---- item 生命周期 ----
    def _close_cur(self) -> list:
        c, self.cur = self.cur, None
        if not c:
            return []
        c["closed"] = True
        evts = []
        if c["kind"] == "text":
            item = {"id": c["id"], "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": c["text"], "annotations": []}]}
            evts.append(self.ev("response.output_text.done", item_id=c["id"],
                                output_index=c["idx"], content_index=0, text=c["text"],
                                logprobs=[]))
            evts.append(self.ev("response.content_part.done", item_id=c["id"],
                                output_index=c["idx"], content_index=0,
                                part={"type": "output_text", "text": c["text"],
                                      "annotations": []}))
        elif c["kind"] == "reasoning":
            item = {"id": c["id"], "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": c["text"]}]}
            evts.append(self.ev("response.reasoning_summary_text.done", item_id=c["id"],
                                output_index=c["idx"], summary_index=0, text=c["text"]))
            evts.append(self.ev("response.reasoning_summary_part.done", item_id=c["id"],
                                output_index=c["idx"], summary_index=0,
                                part={"type": "summary_text", "text": c["text"]}))
        else:                                   # tool
            if c["custom"]:
                evts.append(self.ev("response.custom_tool_call_input.done", item_id=c["id"],
                                    output_index=c["idx"],
                                    input=_unwrap_custom(c["args"])))
            else:
                evts.append(self.ev("response.function_call_arguments.done", item_id=c["id"],
                                    output_index=c["idx"], arguments=c["args"]))
            item = self._tool_item(c, "completed")
        item.pop("_idx", None)
        evts.append(self.ev("response.output_item.done", output_index=c["idx"], item=item))
        self.items.append(item)
        return evts

    def _tool_item(self, c: dict, status: str) -> dict:
        if c["custom"]:
            return {"id": c["id"], "type": "custom_tool_call", "call_id": c["call_id"],
                    "name": c["name"], "input": _unwrap_custom(c["args"]), "status": status}
        item = {"id": c["id"], "type": "function_call", "call_id": c["call_id"],
                "name": c["name"], "arguments": c["args"], "status": status}
        ns = self.ns_of.get(c["name"])
        if ns:
            item["namespace"] = ns
        return item

    def _switch(self, kind: str, key=None) -> list:
        """需要换到另一个 item 了：关掉当前的"""
        if self.cur is None:
            return []
        if self.cur["kind"] == kind and (kind != "tool" or self.cur.get("key") == key):
            return []
        return self._close_cur()

    # ---- 增量 ----
    def _reasoning_delta(self, s: str) -> list:
        evts = self._switch("reasoning")
        if evts or self.cur is None:
            idx = self.out_idx
            self.out_idx += 1
            self.cur = {"kind": "reasoning", "id": _rid("rs_"), "idx": idx, "text": "",
                        "closed": False}
            evts.append(self.ev("response.output_item.added", output_index=idx,
                                item={"id": self.cur["id"], "type": "reasoning", "summary": []}))
            evts.append(self.ev("response.reasoning_summary_part.added", item_id=self.cur["id"],
                                output_index=idx, summary_index=0,
                                part={"type": "summary_text", "text": ""}))
        self.cur["text"] += s
        evts.append(self.ev("response.reasoning_summary_text.delta", item_id=self.cur["id"],
                            output_index=self.cur["idx"], summary_index=0, delta=s))
        return evts

    def _text_delta(self, s: str) -> list:
        evts = self._switch("text")
        if evts or self.cur is None:
            idx = self.out_idx
            self.out_idx += 1
            self.cur = {"kind": "text", "id": _rid("msg_"), "idx": idx, "text": "",
                        "closed": False}
            evts.append(self.ev("response.output_item.added", output_index=idx,
                                item={"id": self.cur["id"], "type": "message", "status": "in_progress",
                                      "role": "assistant", "content": []}))
            evts.append(self.ev("response.content_part.added", item_id=self.cur["id"],
                                output_index=idx, content_index=0,
                                part={"type": "output_text", "text": "", "annotations": []}))
        self.cur["text"] += s
        evts.append(self.ev("response.output_text.delta", item_id=self.cur["id"],
                            output_index=self.cur["idx"], content_index=0, delta=s, logprobs=[]))
        return evts

    def _tool_delta(self, tc: dict) -> list:
        key = tc.get("index")
        if not isinstance(key, int) or isinstance(key, bool):
            key = 0
        c = self.tools.get(key)
        if c is not None and c.get("closed"):
            c = None          # 已收尾的 item 不再续写（防御：不该发生，真发生就另开一个）
        evts = []
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name")
        if c is None:
            evts += self._switch("tool", key)
            idx = self.out_idx
            self.out_idx += 1
            call_id = tc.get("id") if isinstance(tc.get("id"), str) and tc.get("id") \
                else _rid("call_")
            is_custom = isinstance(name, str) and name in self.custom
            c = {"kind": "tool", "key": key, "idx": idx,
                 "id": _rid("ctc_" if is_custom else "fc_"),
                 "call_id": call_id, "name": name if isinstance(name, str) else "",
                 "args": "", "custom": is_custom, "closed": False}
            self.tools[key] = c
            self.cur = c
            evts.append(self.ev("response.output_item.added", output_index=idx,
                                item=self._tool_item(c, "in_progress")))
        else:
            evts += self._switch("tool", key)
            self.cur = c
            # 名字分片到达（罕见但存在）：只有首片算数，后续片段补上
            if isinstance(name, str) and name and name != c["name"]:
                if not c["name"]:
                    c["name"] = name
                    c["custom"] = name in self.custom
                elif not name.startswith(c["name"]):
                    c["name"] += name
        args = fn.get("arguments")
        if isinstance(args, str) and args:
            c["args"] += args
            if not c["custom"]:
                evts.append(self.ev("response.function_call_arguments.delta", item_id=c["id"],
                                    output_index=c["idx"], delta=args))
            # custom tool 的增量是半截 JSON（{"input": "..."）没法直接还原成裸文本，
            # 只在收尾时给一次完整的 custom_tool_call_input.done（见 _close_cur）
        return evts

    # ---- 输入 ----
    def feed(self, chunk: bytes) -> list:
        evts = []
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            evts += self._line(line)
        return evts

    def flush(self) -> list:
        """收尾：最后一行可能没有换行结尾"""
        if not self.buf.strip():
            self.buf = b""
            return []
        line, self.buf = self.buf, b""
        return self._line(line)

    def _line(self, line: bytes) -> list:
        line = line.strip()
        if not line.startswith(b"data:"):
            return []
        payload = line[5:].strip()
        if payload in (b"[DONE]", b"[done]"):
            return []          # 官方 Responses 流不带这个哨兵，吞掉、不转发
        try:
            j = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(j, dict):
            return []
        if isinstance(j.get("usage"), dict) and j["usage"]:
            self.usage = j["usage"]
        if isinstance(j.get("model"), str) and j["model"]:
            self.model = j["model"]

        evts = []
        choices = j.get("choices")
        if not isinstance(choices, list):
            return evts
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta")
            if not isinstance(delta, dict):
                delta = ch.get("message") if isinstance(ch.get("message"), dict) else {}
            rc = _reasoning_text(delta)
            if rc:
                evts += self._reasoning_delta(rc)
            c = delta.get("content")
            if isinstance(c, list):
                c = _text_of_parts(c)
            if not isinstance(c, str) or not c:
                c = ch.get("text") if isinstance(ch.get("text"), str) else ""
            if c:
                evts += self._text_delta(c)
            tcs = delta.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        evts += self._tool_delta(tc)
            if ch.get("finish_reason"):
                self.finish_reason = ch["finish_reason"]
        return evts

    # ---- 收尾 ----
    def finish(self, err: str | None = None) -> list:
        evts = self.flush()
        evts += self._close_cur()
        if err:
            env = self.envelope("failed", error={"code": "upstream_stream_error",
                                                 "message": err[:400]})
            evts.append(self.ev("response.failed", response=env))
            return evts
        status = "incomplete" if self.finish_reason in ("length", "content_filter") else "completed"
        if status == "incomplete":
            evts.append(self.ev("response.incomplete", response=self.envelope(status)))
        else:
            evts.append(self.ev("response.completed", response=self.envelope(status)))
        return evts


async def translate_stream(inner, ctx: dict):
    """chat 的 SSE 字节流 → Responses 的事件流。

    `inner` 就是 `main._stream_gen` 那个生成器（由 `_chat_v1` 包在 StreamingResponse 里）。
    它自己已经在做记账（usage 抽取、成败判定、TTFT），这里只旁路解析同一条字节流，
    **不改变**它看到的任何东西 —— 顺序仍是「先解析再 yield」，客户端断开时
    GeneratorExit 会原样穿透到它那里。
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
    except (GeneratorExit, asyncio.CancelledError):
        raise                      # 客户端断开 / 服务停机：交给上游生成器走它自己的收尾
    except Exception as e:         # 上游断流：别把异常甩给客户端，改发 response.failed
        err = f"{type(e).__name__}: {e}"
        logger.warning("responses: 上游流中断，回 response.failed: %s", err[:180])
    for e in st.finish(err):
        yield e


# ---------------------------------------------------------------- 出口

def _hub_headers(resp) -> dict:
    """只搬运自家的 X-Api-Hub-* 头。

    不能整份复制：JSONResponse 带 content-length / content-type，照搬会让新响应的
    长度或类型对不上（body 已经变了）。"""
    try:
        items = resp.headers.items()
    except AttributeError:
        return {}
    return {k: v for k, v in items if k.lower().startswith("x-api-hub-")}


def _json_body(resp) -> dict:
    try:
        raw = bytes(getattr(resp, "body", b"") or b"")
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}


async def convert(resp, ctx: dict):
    """`_chat_v1` 的返回值 → Responses 协议的返回值（流式/非流式同一个入口）"""
    headers = _hub_headers(resp)
    if isinstance(resp, StreamingResponse):
        headers["Cache-Control"] = "no-cache"
        return StreamingResponse(translate_stream(resp.body_iterator, ctx),
                                 media_type="text/event-stream", headers=headers)
    return JSONResponse(build_response(_json_body(resp), ctx), headers=headers)
