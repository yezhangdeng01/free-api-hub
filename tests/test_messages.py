"""`/v1/messages`（Anthropic 方言）翻译层测试（离线：不打外网、不占端口）。

分四组：
1. 请求翻译 —— system / 块数组 / 图片 / tools / tool_choice / max_tokens 必填
2. 非流式回包 —— chat 响应 → Anthropic message 对象
3. 流式回包 —— chat SSE → Anthropic 事件流（顺序 + 文本/工具增量拼接）
4. 端到端 —— TestClient 打 `/v1/messages`，上游用假 httpx client 打桩

**最要紧的一条**：`test_tool_use_round_trip_preserves_id` 保证多轮工具调用能闭合
（请求方向的 `tool_use_id` 与响应方向的 `tool_use.id` 必须是同一个值）。
改翻译层前先把这条跑一遍。
"""
import asyncio
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gateway, store  # noqa: E402
from app.dialects import messages as M  # noqa: E402
from app.main import app  # noqa: E402

H = {"host": "127.0.0.1:8787"}


# ---------------------------------------------------------------- 1. 请求翻译

def test_system_and_blocks_become_messages():
    chat, ctx = M.to_chat({
        "model": "glm-test",
        "system": "你是助手",
        "max_tokens": 100,
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": "你好"},
                                  {"type": "text", "text": "再见"}]}],
    })
    assert chat["model"] == "glm-test"
    assert chat["max_tokens"] == 100
    assert chat["messages"][0] == {"role": "system", "content": "你是助手"}
    # 纯文本块折叠成字符串（兼容层多数只认字符串）
    assert chat["messages"][1] == {"role": "user", "content": "你好再见"}


def test_system_as_blocks():
    chat, _ = M.to_chat({"model": "m", "max_tokens": 1,
                         "system": [{"type": "text", "text": "规则A"}],
                         "messages": [{"role": "user", "content": "hi"}]})
    assert chat["messages"][0] == {"role": "system", "content": "规则A"}


def test_image_block_keeps_array_and_data_url():
    chat, _ = M.to_chat({"model": "m", "max_tokens": 1, "messages": [{
        "role": "user",
        "content": [{"type": "text", "text": "这是什么"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": "AAA"}}],
    }]})
    content = chat["messages"][0]["content"]
    assert isinstance(content, list), "带图时必须保留块数组形态"
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAA"


def test_tool_result_becomes_role_tool_with_same_id():
    """请求方向：tool_result.tool_use_id → role:"tool".tool_call_id（id 不能变）"""
    chat, _ = M.to_chat({"model": "m", "max_tokens": 10, "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "我查一下"},
                                          {"type": "tool_use", "id": "toolu_abc",
                                           "name": "get_weather", "input": {"city": "北京"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_abc",
                                      "content": "晴 18 度"}]},
    ]})
    a, t = chat["messages"][0], chat["messages"][1]
    assert a["tool_calls"][0]["id"] == "toolu_abc"
    assert a["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(a["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}
    assert t == {"role": "tool", "tool_call_id": "toolu_abc", "content": "晴 18 度"}


def test_tool_result_with_block_content():
    chat, _ = M.to_chat({"model": "m", "max_tokens": 1, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": [{"type": "text", "text": "结果A"},
                                                  {"type": "text", "text": "结果B"}]}]},
    ]})
    assert chat["messages"][0]["content"] == "结果A结果B"


def test_tools_and_tool_choice():
    chat, _ = M.to_chat({
        "model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "f", "description": "d",
                   "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}}}],
        "tool_choice": {"type": "any"},
    })
    assert chat["tools"][0]["function"]["name"] == "f"
    assert chat["tools"][0]["function"]["parameters"]["properties"]["a"]["type"] == "string"
    assert chat["tool_choice"] == "required"      # any → required（见实现注释）


def test_stop_sequences_and_sampling_fields():
    chat, _ = M.to_chat({"model": "m", "max_tokens": 5, "temperature": 0.3, "top_p": 0.9,
                         "top_k": 20, "stop_sequences": ["###"],
                         "messages": [{"role": "user", "content": "hi"}]})
    assert chat["stop"] == ["###"]
    assert chat["temperature"] == 0.3 and chat["top_p"] == 0.9 and chat["top_k"] == 20


def test_streaming_sets_stream_and_usage_option():
    chat, _ = M.to_chat({"model": "m", "max_tokens": 5, "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]})
    assert chat["stream"] is True
    assert chat["stream_options"] == {"include_usage": True}


def test_thinking_blocks_are_dropped_and_logged():
    chat, ctx = M.to_chat({"model": "m", "max_tokens": 5,
                           "thinking": {"type": "enabled", "budget_tokens": 100},
                           "messages": [{"role": "assistant", "content": [
                               {"type": "thinking", "thinking": "内部推理", "signature": "x"},
                               {"type": "text", "text": "答案"}]}]})
    assert "thinking" in ctx["dropped"]
    assert chat["messages"][0]["content"] == "答案"      # 思考块不往上游送


@pytest.mark.parametrize("body,keyword", [
    ({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}, "model"),
    ({"model": "m", "max_tokens": 1}, "messages"),
    ({"model": "m", "max_tokens": 1, "messages": []}, "messages"),
    ({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "max_tokens"),
    ({"model": "m", "max_tokens": 0, "messages": [{"role": "user", "content": "hi"}]},
     "max_tokens"),
])
def test_client_errors_raise_value_error(body, keyword):
    with pytest.raises(ValueError) as e:
        M.to_chat(body)
    assert keyword in str(e.value)


# ---------------------------------------------------------------- 2. 非流式回包

def test_build_message_text_and_usage():
    out = M.build_message({
        "id": "chatcmpl-1", "model": "glm-test",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "你好"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }, {"model": "glm-test"})
    assert out["type"] == "message" and out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "你好"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_build_message_length_maps_to_max_tokens():
    out = M.build_message({"choices": [{"finish_reason": "length",
                                        "message": {"content": "半截"}}]}, {"model": "m"})
    assert out["stop_reason"] == "max_tokens"


def test_build_message_tool_calls_become_tool_use():
    out = M.build_message({
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "f", "arguments": '{"a":1}'}}]}}],
    }, {"model": "m"})
    assert out["stop_reason"] == "tool_use"
    assert out["content"] == [{"type": "tool_use", "id": "call_1", "name": "f", "input": {"a": 1}}]


_TOOL_CALL = {"id": "call_1", "type": "function",
              "function": {"name": "f", "arguments": "{}"}}


@pytest.mark.parametrize("finish_reason,with_tools,expect", [
    # 回包里有 tool_use 块：finish_reason 缺失 / None / 写成 stop / 编成别的值，
    # 都不能报 end_turn —— 否则客户端当这一轮正常结束，工具不会被执行
    ("<MISSING>", True, "tool_use"),
    (None, True, "tool_use"),
    ("stop", True, "tool_use"),
    ("foo", True, "tool_use"),
    ("tool_calls", True, "tool_use"),
    # 截断优先：参数可能只写了一半，不能让客户端照着执行
    ("length", True, "max_tokens"),
    # 没有工具调用的常规路径不受影响
    ("stop", False, "end_turn"),
    ("length", False, "max_tokens"),
    ("<MISSING>", False, "end_turn"),
    (None, False, "end_turn"),
    ("foo", False, "end_turn"),
    ("tool_calls", False, "tool_use"),
])
def test_build_message_stop_reason_precedence(finish_reason, with_tools, expect):
    ch = {"message": {"role": "assistant", "content": ""}}
    if with_tools:
        ch["message"]["tool_calls"] = [dict(_TOOL_CALL)]
    if finish_reason != "<MISSING>":
        ch["finish_reason"] = finish_reason
    out = M.build_message({"choices": [ch]}, {"model": "m"})
    assert out["stop_reason"] == expect


def test_build_message_never_empty_content():
    """空回复也要给一个空 text 块：Anthropic 的 content 不允许空数组"""
    out = M.build_message({"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
                          {"model": "m"})
    assert out["content"] == [{"type": "text", "text": ""}]


# ---------------------------------------------------------------- 3. 流式回包

def _sse_lines(*objs):
    out = []
    for o in objs:
        if o == "[DONE]":
            out.append(b"data: [DONE]\n")
        else:
            out.append(b"data: " + json.dumps(o).encode() + b"\n")
    return out


async def _feed(state, lines):
    evts = []
    for ln in lines:
        evts += state.feed(ln)
    return evts


def _parse(frames):
    out = []
    for f in frames:
        text = f.decode("utf-8")
        name = ""
        data = ""
        for line in text.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        out.append((name, json.loads(data)))
    return out


def test_stream_event_order_and_text_deltas():
    st = M._StreamState({"model": "glm-test"})
    frames = st.start()
    frames += asyncio.run(_feed(st, _sse_lines(
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        "[DONE]",
    )))
    frames += st.finish()

    names = [n for n, _ in _parse(frames)]
    # 顺序：message_start → block_start → delta* → block_stop → message_delta → message_stop
    assert names[0] == "message_start"
    assert names.index("content_block_start") < names.index("content_block_delta")
    assert names.index("content_block_stop") < names.index("message_delta")
    assert names[-2:] == ["message_delta", "message_stop"]

    parsed = _parse(frames)
    deltas = [d["delta"]["text"] for n, d in parsed if n == "content_block_delta"]
    assert deltas == ["你", "好"]
    stop = [d for n, d in parsed if n == "message_delta"][0]
    assert stop["delta"]["stop_reason"] == "end_turn"
    assert stop["usage"]["output_tokens"] == 2
    # usage 块不能变成内容
    assert all(d.get("type") != "text_delta" for n, d in parsed
               if n == "content_block_delta" and "usage" in d)


def test_stream_tool_input_json_deltas_are_split_but_reassemble():
    st = M._StreamState({"model": "m"})
    st.start()
    frames = asyncio.run(_feed(st, _sse_lines(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_9", "function": {"name": "f", "arguments": '{"a"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ":1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )))
    frames += st.finish()
    parsed = _parse(frames)
    start = [d for n, d in parsed if n == "content_block_start"][0]
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["id"] == "call_9"
    assert start["content_block"]["name"] == "f"
    partials = [d["delta"]["partial_json"] for n, d in parsed if n == "content_block_delta"]
    assert partials == ['{"a"', ":1}"]
    assert json.loads("".join(partials)) == {"a": 1}          # 碎片能拼回合法 JSON
    assert [d["delta"]["stop_reason"] for n, d in parsed if n == "message_delta"] == ["tool_use"]


def test_stream_text_then_tool_closes_text_block_first():
    st = M._StreamState({"model": "m"})
    st.start()
    frames = asyncio.run(_feed(st, _sse_lines(
        {"choices": [{"delta": {"content": "先说一句"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
    )))
    frames += st.finish()
    parsed = _parse(frames)
    starts = [d["index"] for n, d in parsed if n == "content_block_start"]
    stops = [d["index"] for n, d in parsed if n == "content_block_stop"]
    assert starts == [0, 1]                    # 文本块 0、工具块 1
    assert stops == [0, 1]                     # 先关文本再关工具


def _block_blocks(parsed):
    """把事件流折成 [(index, delta 个数)]，同时断言「一次只开一个块」。

    Anthropic 的硬要求：每个 content block 自己 start → delta* → stop 走完才轮到下一个。
    顺序错了事件流仍然能被读出来（官方 SDK 宽容），所以只有这里是唯一能把违规钉住的地方。
    """
    cur, blocks = None, []
    for name, d in parsed:
        if name == "content_block_start":
            assert cur is None, f"block {d['index']} 开的时候上一个（index={cur}）还没关"
            cur = d["index"]
            blocks.append([cur, 0])
        elif name == "content_block_delta":
            assert cur == d["index"], f"delta 落在 index={d['index']}，但开着的是 {cur}"
            blocks[-1][1] += 1
        elif name == "content_block_stop":
            assert cur == d["index"], f"stop 的是 index={d['index']}，但开着的是 {cur}"
            cur = None
    assert cur is None, "流结束时还有 block 没收尾"
    return [(i, n) for i, n in blocks]


def test_stream_parallel_tool_calls_close_each_block_before_next():
    """并行工具调用（chat 用 index 0/1 表达）要拆成「块 1 整段 → 块 2 整段」。

    这里是曾经的漏网之鱼：两个工具块的 `content_block_start` 挤在一起，两个 `stop` 统一堆到
    流末尾。官方 SDK 能容忍，但顺序是错的。
    """
    st = M._StreamState({"model": "m"})
    st.start()
    frames = asyncio.run(_feed(st, _sse_lines(
        {"choices": [{"delta": {"content": "先查一下"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "get_weather",
                                                      "arguments": '{"city":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"北京"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_b", "function": {"name": "get_time",
                                                      "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )))
    frames += st.finish()
    parsed = _parse(frames)
    assert _block_blocks(parsed) == [(0, 1), (1, 2), (2, 1)]

    starts = [d for n, d in parsed if n == "content_block_start"]
    assert [s["content_block"]["type"] for s in starts] == ["text", "tool_use", "tool_use"]
    assert [s["content_block"]["id"] for s in starts[1:]] == ["call_a", "call_b"]

    frag = [(d["index"], d["delta"]["partial_json"]) for n, d in parsed
            if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta"]
    assert frag == [(1, '{"city":'), (1, '"北京"}'), (2, "{}")]
    assert json.loads("".join(x for i, x in frag if i == 1)) == {"city": "北京"}
    assert [d["delta"]["stop_reason"] for n, d in parsed if n == "message_delta"] == ["tool_use"]


def test_stream_tool_then_text_closes_tool_block_first():
    """工具块之后又出文本（少数渠道会这样）：必须先把工具块关掉，再开文本块。

    旧写法会把两者都留着，收尾时 stop 顺序变成 [1, 0] —— 与 start 顺序相反。
    """
    st = M._StreamState({"model": "m"})
    st.start()
    frames = asyncio.run(_feed(st, _sse_lines(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {"content": "补充说明"}}]},
        "[DONE]",
    )))
    frames += st.finish()
    parsed = _parse(frames)
    assert _block_blocks(parsed) == [(0, 1), (1, 1)]
    starts = [d for n, d in parsed if n == "content_block_start"]
    assert [s["content_block"]["type"] for s in starts] == ["tool_use", "text"]


def test_stream_missing_finish_reason_with_tools_is_tool_use():
    """上游没给 finish_reason（免额渠道常见）但回了工具调用 → 必须是 tool_use。

    报成 end_turn 的话，客户端会认为这一轮正常结束，工具永远不会被执行。
    """
    st = M._StreamState({"model": "m"})
    st.start()
    frames = asyncio.run(_feed(st, _sse_lines(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
        "[DONE]",
    )))
    frames += st.finish()
    parsed = _parse(frames)
    assert st.finish_reason is None
    assert [d["delta"]["stop_reason"] for n, d in parsed if n == "message_delta"] == ["tool_use"]


def test_stream_upstream_error_emits_error_event():
    st = M._StreamState({"model": "m"})
    st.start()
    frames = st.finish("RuntimeError: boom")
    parsed = _parse(frames)
    assert parsed[-1][0] == "error"
    assert parsed[-1][1]["error"]["type"] == "api_error"


# ---------------------------------------------------------------- 4. 端到端

def _wire_upstream(monkeypatch, model="glm-test", chat_data=None, sse=b"", status=200):
    """把 `main` 的共享 httpx client 换成假的，渠道也伪造好。
    返回捕获到的上游请求体列表（断言翻译后的 chat body 真的发出去了）。"""
    from app import config as cfgmod
    from app import main as m

    chans = [{"id": "t_c1", "name": "A", "type": "custom",
              "base_url": "http://a.invalid/v1", "api_key": "k", "enabled": True}]
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "channels": chans, "aliases": {}, "route_strategy": "balanced", "pinned": [],
        "auth_enabled": False, "api_token": "", "port": 8787})
    gateway.channels.clear()
    cs = gateway.ChannelState()
    cs.models, cs.valid, cs.latency_ms = [model], True, 10
    gateway.channels["t_c1"] = cs

    seen = []

    class FakeResp:
        def __init__(self):
            self.status_code = status
            self.headers = {}

        def json(self):
            return chat_data

        async def aread(self):
            return b""

        async def aiter_bytes(self):
            for c in sse:
                yield c

        async def aclose(self):
            pass

    class FakeClient:
        def build_request(self, method, url, json=None, headers=None):
            seen.append(json)
            return ("req", json)

        async def send(self, req, stream=False):
            return FakeResp()

        async def post(self, url, json=None, headers=None):
            seen.append(json)
            return FakeResp()

        async def aclose(self):
            pass

    monkeypatch.setattr(m, "shared_client", FakeClient())
    return seen


@pytest.fixture
def client(tmp_path_factory):
    """与 test_responses.py 同款隔离：落盘路径改到临时目录，别让测试收尾写生产文件。"""
    from app import store as _store
    tmp = tmp_path_factory.mktemp("messages-state")
    old = (_store.DB_PATH, _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH,
           _store.CAPABILITY_CACHE_PATH)
    _store.DB_PATH = str(tmp / "usage.db")
    _store.RUNTIME_STATE_PATH = str(tmp / "runtime_state.json")
    _store.MODEL_STATUS_PATH = str(tmp / "model_status.json")
    _store.CAPABILITY_CACHE_PATH = str(tmp / "capability_cache.json")
    _store._ms_cache = None
    try:
        with TestClient(app, headers=H) as c:
            yield c
    finally:
        (_store.DB_PATH, _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH,
         _store.CAPABILITY_CACHE_PATH) = old
        _store._ms_cache = None


def test_e2e_non_stream(client, monkeypatch):
    seen = _wire_upstream(monkeypatch, chat_data={
        "id": "c1", "model": "glm-test",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1},
    })
    r = client.post("/v1/messages", headers=H, json={
        "model": "glm-test", "max_tokens": 64,
        "system": "简短回答", "messages": [{"role": "user", "content": "ping"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "pong"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 4
    assert r.headers.get("anthropic-version") == M.ANTHROPIC_VERSION
    # 翻译后的 chat body 真的发出去了
    assert seen and seen[0]["model"] == "glm-test"
    assert seen[0]["messages"][0] == {"role": "system", "content": "简短回答"}
    assert seen[0]["max_tokens"] == 64


def test_e2e_missing_max_tokens_is_anthropic_error(client, monkeypatch):
    _wire_upstream(monkeypatch, chat_data={"choices": []})
    r = client.post("/v1/messages", headers=H,
                    json={"model": "glm-test", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert r.json()["type"] == "error"
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert "max_tokens" in r.json()["error"]["message"]


def test_e2e_bad_json_is_anthropic_error(client, monkeypatch):
    _wire_upstream(monkeypatch, chat_data={"choices": []})     # 顺带解除鉴权（见 fixture 说明）
    r = client.post("/v1/messages", headers={**H, "content-type": "application/json"},
                    content=b"{not json")
    assert r.status_code == 400
    assert r.json()["type"] == "error"


def test_e2e_stream(client, monkeypatch):
    chunks = _sse_lines(
        {"choices": [{"delta": {"content": "pong"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        "[DONE]",
    )
    _wire_upstream(monkeypatch, sse=[c + b"\n" for c in chunks])
    r = client.post("/v1/messages", headers=H, json={
        "model": "glm-test", "max_tokens": 32, "stream": True,
        "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    text = r.text
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text
    assert "[DONE]" not in text               # chat 的哨兵不能漏给 Anthropic 客户端


def test_e2e_tool_use_round_trip_preserves_id(client, monkeypatch):
    """多轮工具调用的命门：响应里的 tool_use.id 与下一轮请求的 tool_use_id 必须是同一个值。"""
    seen = _wire_upstream(monkeypatch, chat_data={
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "toolu_keep_me", "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]}}],
    })
    r1 = client.post("/v1/messages", headers=H, json={
        "model": "glm-test", "max_tokens": 64,
        "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "北京天气"}]})
    assert r1.status_code == 200, r1.text
    block = r1.json()["content"][0]
    assert block["type"] == "tool_use" and block["id"] == "toolu_keep_me"

    # 把回包原样回灌（客户端就是这么做的）
    r2 = client.post("/v1/messages", headers=H, json={
        "model": "glm-test", "max_tokens": 64,
        "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": [block]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block["id"],
                                          "content": "晴 18 度"}]},
        ]})
    assert r2.status_code == 200, r2.text
    sent = seen[-1]
    assert sent["messages"][1]["tool_calls"][0]["id"] == "toolu_keep_me"
    assert sent["messages"][2] == {"role": "tool", "tool_call_id": "toolu_keep_me",
                                   "content": "晴 18 度"}
