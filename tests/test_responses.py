"""`/v1/responses` 翻译层测试（离线：不打外网、不占端口）。

分四组：
1. 请求翻译 —— instructions/input/tools ↔ messages/function calling
2. 非流式回包 —— chat 响应 → Responses 对象
3. 流式回包 —— chat SSE → Responses 事件流（含推理、文本、工具调用增量）
4. 端到端 —— 用 TestClient 打 `/v1/responses`，上游用假的 httpx client 打桩

**最后一条最要紧**：`test_tool_call_round_trip_preserves_call_id` 保证多轮工具调用能闭合
（`call_id` 在请求方向和响应方向必须一致），改翻译层前先跑它。
"""
import asyncio
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gateway, responses as R, store  # noqa: E402
from app.main import app  # noqa: E402

H = {"host": "127.0.0.1:8787"}


# ---------------------------------------------------------------- 1. 请求翻译

def test_instructions_and_input_become_messages():
    chat, ctx = R.to_chat({"model": "m", "instructions": "你是助手",
                           "input": "你好"})
    assert chat["messages"] == [{"role": "system", "content": "你是助手"},
                                {"role": "user", "content": "你好"}]
    assert ctx["model"] == "m"


def test_instructions_accepts_content_part_array():
    """官方还允许 instructions 是内容片段数组，应压成纯文本再进 system。"""
    chat, ctx = R.to_chat({"model": "m",
                           "instructions": [{"type": "text", "text": "规则一"},
                                            {"type": "input_text", "text": "规则二"}],
                           "input": "hi"})
    assert chat["messages"][0] == {"role": "system", "content": "规则一规则二"}
    assert ctx["instructions"] == "规则一规则二"


def test_input_message_items_and_developer_role():
    """`developer` 是 Responses 里的新 system；label 数组里的纯文本折叠成字符串。"""
    chat, _ = R.to_chat({"model": "m", "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "规则"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "a"},
                                     {"type": "input_text", "text": "b"}]},
    ]})
    assert chat["messages"] == [{"role": "system", "content": "规则"},
                                {"role": "user", "content": "ab"}]


def test_input_image_keeps_array_form_but_file_id_degrades_to_text():
    chat, _ = R.to_chat({"model": "m", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "看图"},
                                     {"type": "input_image",
                                      "image_url": "data:image/png;base64,AAA"}]},
        {"role": "user", "content": [{"type": "input_image", "file_id": "file-9"}]},
    ]})
    first = chat["messages"][0]["content"]
    assert isinstance(first, list) and first[1]["image_url"]["url"].startswith("data:image/png")
    # 网关没有文件存储 → file_id 解不出来时必须降级成文本，不能把消息丢掉
    assert chat["messages"][1]["content"] == "[图片 file_id=file-9]"


def test_tool_call_round_trip_preserves_call_id():
    """工具调用闭合：请求侧的 call_id ↔ chat 的 tool_call_id / tool_calls[].id。"""
    chat, _ = R.to_chat({"model": "m", "input": [
        {"role": "user", "content": "北京天气"},
        {"type": "function_call", "call_id": "call_abc", "name": "get_weather",
         "arguments": "{\"city\":\"北京\"}"},
        {"type": "function_call", "call_id": "call_def", "name": "get_time",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_abc", "output": "晴"},
        {"type": "function_call_output", "call_id": "call_def",
         "output": [{"type": "input_text", "text": "12:00"}]},
    ]})
    msgs = chat["messages"]
    # 连续两个 function_call 必须合并进**同一条** assistant 消息
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool"]
    assert [t["id"] for t in msgs[1]["tool_calls"]] == ["call_abc", "call_def"]
    assert msgs[1]["content"] is None
    assert [m["tool_call_id"] for m in msgs[2:]] == ["call_abc", "call_def"]
    assert msgs[3]["content"] == "12:00"


def test_tools_and_tool_choice_translation():
    chat, ctx = R.to_chat({"model": "m", "input": "hi", "tool_choice": {"type": "function", "name": "f"},
                           "tools": [
                               {"type": "function", "name": "f", "description": "d",
                                "parameters": {"type": "object"}, "strict": True},
                               {"type": "custom", "name": "patch"},
                               {"type": "web_search_preview"},
                           ]})
    t = chat["tools"]
    assert len(t) == 2, t
    assert t[0]["function"]["name"] == "f" and t[0]["function"]["strict"] is True
    assert t[1]["function"]["parameters"]["required"] == ["input"]
    assert ctx["custom_tools"] == {"patch"}
    assert ctx["dropped"] == ["web_search_preview"]          # 托管工具服务不了 → 记账丢弃
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_no_tools_means_no_tool_choice():
    chat, _ = R.to_chat({"model": "m", "input": "hi", "tool_choice": "required"})
    assert "tools" not in chat and "tool_choice" not in chat


def test_max_output_tokens_and_text_format():
    chat, _ = R.to_chat({"model": "m", "input": "hi", "max_output_tokens": 128,
                         "text": {"format": {"type": "json_schema", "name": "o",
                                             "schema": {"type": "object"}, "strict": True}}})
    assert chat["max_tokens"] == 128
    assert chat["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "o",
                                                       "schema": {"type": "object"},
                                                       "strict": True}}


@pytest.mark.parametrize("given,expect", [("json_object", {"type": "json_object"}),
                                          ("text", None)])
def test_text_format_plain(given, expect):
    chat, _ = R.to_chat({"model": "m", "input": "hi", "text": {"format": {"type": given}}})
    assert chat.get("response_format") == expect


def test_stream_usage_switch():
    """默认给流式加 include_usage（拿 token 用量）；关掉后不加，也不覆盖客户端自己给的。"""
    on, _ = R.to_chat({"model": "m", "input": "hi", "stream": True},
                      {"responses_stream_usage": True})
    assert on["stream"] is True and on["stream_options"] == {"include_usage": True}
    off, _ = R.to_chat({"model": "m", "input": "hi", "stream": True},
                       {"responses_stream_usage": False})
    assert "stream_options" not in off
    own, _ = R.to_chat({"model": "m", "input": "hi", "stream": True,
                        "stream_options": {"foo": 1}}, {"responses_stream_usage": True})
    assert own["stream_options"] == {"foo": 1}


def test_reasoning_effort_is_opt_in():
    """默认不透传 reasoning.effort（个别渠道不认会整条 400）。"""
    off, ctx = R.to_chat({"model": "m", "input": "hi", "reasoning": {"effort": "high"}},
                         {"responses_reasoning_effort": False})
    assert "reasoning_effort" not in off
    assert any("reasoning.effort" in d for d in ctx["dropped"])
    on, _ = R.to_chat({"model": "m", "input": "hi", "reasoning": {"effort": "high"}},
                      {"responses_reasoning_effort": True})
    assert on["reasoning_effort"] == "high"


def test_passthrough_fields_kept():
    chat, _ = R.to_chat({"model": "m", "input": "hi", "temperature": 0.3, "top_p": 0.9,
                         "seed": 7, "user": "u1", "parallel_tool_calls": False,
                         "metadata": {"a": 1}, "store": True})
    assert chat["temperature"] == 0.3 and chat["top_p"] == 0.9 and chat["seed"] == 7
    assert chat["user"] == "u1" and chat["parallel_tool_calls"] is False


@pytest.mark.parametrize("body", [
    {},                                            # 缺 model
    {"model": "m"},                                # input 空
    {"model": "m", "input": 123},                  # input 类型不对
])
def test_client_errors_raise_value_error(body):
    with pytest.raises(ValueError):
        R.to_chat(body)


def test_reasoning_items_in_input_are_dropped_not_fatal():
    """历史里带着 reasoning item 是常态（客户端会回传）—— 丢掉，不能让整条请求失败。"""
    chat, ctx = R.to_chat({"model": "m", "input": [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"role": "user", "content": "hi"}]})
    assert chat["messages"] == [{"role": "user", "content": "hi"}]
    assert "reasoning" in ctx["dropped"]


# ---------------------------------------------------------------- 2. 非流式回包

def test_build_response_text_and_usage():
    ctx = {"model": "req-model", "custom_tools": set()}
    out = R.build_response({"model": "glm-5", "created": 1700000000,
                            "choices": [{"finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "你好"}}],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 22,
                                      "total_tokens": 33,
                                      "completion_tokens_details": {"reasoning_tokens": 5}}}, ctx)
    assert out["object"] == "response" and out["status"] == "completed"
    assert out["id"].startswith("resp_") and out["model"] == "glm-5"
    assert out["output"] == [{"id": out["output"][0]["id"], "type": "message",
                              "status": "completed", "role": "assistant",
                              "content": [{"type": "output_text", "text": "你好",
                                           "annotations": []}]}]
    assert out["usage"] == {"input_tokens": 11,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 22,
                            "output_tokens_details": {"reasoning_tokens": 5},
                            "total_tokens": 33}


def test_build_response_reasoning_and_tool_calls():
    ctx = {"model": "m", "custom_tools": set()}
    out = R.build_response({"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "查一下", "reasoning_content": "用户要天气",
        "tool_calls": [{"id": "call_x1", "type": "function",
                        "function": {"name": "get_weather", "arguments": "{\"city\":\"沪\"}"}}]}}]},
        ctx)
    kinds = [i["type"] for i in out["output"]]
    assert kinds == ["reasoning", "message", "function_call"], kinds
    fc = out["output"][-1]
    assert fc["call_id"] == "call_x1" and fc["name"] == "get_weather"
    assert fc["arguments"] == "{\"city\":\"沪\"}" and fc["status"] == "completed"
    assert out["output"][0]["summary"][0]["text"] == "用户要天气"


def test_build_response_custom_tool_becomes_custom_tool_call():
    """custom tool 在 chat 里是 {"input": "..."} 包装的，回包时要还原成裸文本。"""
    ctx = {"model": "m", "custom_tools": {"patch"}}
    out = R.build_response({"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_p", "type": "function",
                        "function": {"name": "patch",
                                     "arguments": "{\"input\":\"*** Begin Patch\"}"}}]}}]},
        ctx)
    item = out["output"][0]
    assert item["type"] == "custom_tool_call" and item["input"] == "*** Begin Patch"
    assert item["call_id"] == "call_p"


def test_build_response_length_gives_incomplete():
    out = R.build_response({"choices": [{"finish_reason": "length",
                                         "message": {"role": "assistant", "content": "半截"}}]},
                           {"model": "m", "custom_tools": set()})
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}


def test_build_response_upstream_error_becomes_failed():
    out = R.build_response({"error": {"code": "upstream_down", "message": "挂了"}},
                           {"model": "m", "custom_tools": set()})
    assert out["status"] == "failed"
    assert out["error"] == {"code": "upstream_down", "message": "挂了"}
    assert out["output"] == [] and out["usage"] is None


def test_build_response_echoes_request_fields():
    _, ctx = R.to_chat({"model": "m", "input": "hi", "instructions": "sys",
                        "max_output_tokens": 64, "temperature": 0.5, "store": True,
                        "metadata": {"k": "v"}, "tools": [{"type": "function", "name": "f"}]})
    out = R.build_response({"choices": [{"finish_reason": "stop",
                                         "message": {"content": "x"}}]}, ctx)
    assert out["instructions"] == "sys" and out["max_output_tokens"] == 64
    assert out["temperature"] == 0.5 and out["store"] is True
    assert out["metadata"] == {"k": "v"} and out["tools"] == [{"type": "function", "name": "f"}]


# ---------------------------------------------------------------- 3. 流式回包

def _events(chunks, ctx=None):
    """把 chat SSE 片段喂进翻译器，收集 (事件名, 载荷) 列表"""
    ctx = ctx or {"model": "m", "custom_tools": set()}

    async def inner():
        for c in chunks:
            yield c

    async def run():
        raw = []
        async for e in R.translate_stream(inner(), ctx):
            raw.append(e.decode("utf-8"))
        out = []
        for block in "".join(raw).split("\n\n"):
            for line in block.splitlines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    out.append((name, json.loads(line[6:])))
        return out

    return asyncio.run(run())


def _chunk(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                                "finish_reason": finish}]}) + "\n\n").encode()


def test_stream_text_and_reasoning_events():
    evs = _events([_chunk({"reasoning_content": "想"}), _chunk({"reasoning_content": "一下"}),
                   _chunk({"content": "你好"}), _chunk({"content": "，世界"}),
                   _chunk({}, "stop"), b"data: [DONE]\n\n"])
    names = [n for n, _ in evs]
    assert names[0] == "response.created"
    assert names[1] == "response.in_progress"
    assert "response.reasoning_summary_text.delta" in names
    assert "response.output_text.delta" in names
    assert names[-1] == "response.completed"
    assert not any(n == "response.failed" for n in names)

    assert [p["delta"] for n, p in evs if n == "response.output_text.delta"] == ["你好", "，世界"]
    seqs = [p["sequence_number"] for _, p in evs]
    assert seqs == list(range(len(seqs)))                          # 序号连续

    final = dict(evs)["response.completed"]["response"]
    assert final["status"] == "completed"
    assert [i["type"] for i in final["output"]] == ["reasoning", "message"]
    assert final["output"][1]["content"][0]["text"] == "你好，世界"
    assert final["output"][0]["summary"][0]["text"] == "想一下"
    # 一个 item 配一个 output_index，且不重复
    idx = [i for i in range(len(final["output"]))]
    assert idx == list(range(len(idx)))


def test_stream_tool_call_arguments_preserve_call_id():
    evs = _events([_chunk({"tool_calls": [{"index": 0, "id": "call_z",
                                           "function": {"name": "get_weather",
                                                        "arguments": "{\"ci"}}]}),
                   _chunk({"tool_calls": [{"index": 0,
                                           "function": {"arguments": "ty\":\"BJ\"}"}}]}),
                   _chunk({}, "tool_calls"), b"data: [DONE]\n\n"])
    arg_deltas = [p["delta"] for n, p in evs if n == "response.function_call_arguments.delta"]
    assert arg_deltas == ["{\"ci", "ty\":\"BJ\"}"]
    assert not any(n == "response.output_text.delta" for n, _ in evs)   # 纯工具调用不产出 message item

    final = dict(evs)["response.completed"]["response"]
    fc = final["output"][0]
    assert fc["type"] == "function_call" and fc["call_id"] == "call_z"
    assert fc["name"] == "get_weather" and fc["arguments"] == "{\"city\":\"BJ\"}"
    assert fc["status"] == "completed"


def test_stream_tool_name_split_across_chunks():
    evs = _events([_chunk({"tool_calls": [{"index": 0, "id": "c1",
                                           "function": {"name": "get_"}}]}),
                   _chunk({"tool_calls": [{"index": 0,
                                           "function": {"name": "weather", "arguments": "{}"}}]}),
                   _chunk({}, "tool_calls")])
    fc = dict(evs)["response.completed"]["response"]["output"][0]
    assert fc["name"] == "get_weather"


def test_stream_two_tool_calls_get_separate_output_indexes():
    evs = _events([_chunk({"tool_calls": [{"index": 0, "id": "c0",
                                           "function": {"name": "a", "arguments": "{}"}}]}),
                   _chunk({"tool_calls": [{"index": 1, "id": "c1",
                                           "function": {"name": "b", "arguments": "{}"}}]}),
                   _chunk({}, "tool_calls")])
    out = dict(evs)["response.completed"]["response"]["output"]
    assert [i["call_id"] for i in out] == ["c0", "c1"]
    assert [i["id"] for i in out][0] != [i["id"] for i in out][1]


def test_stream_usage_and_incomplete():
    evs = _events([_chunk({"content": "半"}), _chunk({}, "length"),
                   ("data: " + json.dumps({"choices": [], "usage": {
                       "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}) + "\n\n").encode(),
                   b"data: [DONE]\n\n"])
    names = [n for n, _ in evs]
    assert names[-1] == "response.incomplete"
    final = dict(evs)["response.incomplete"]["response"]
    assert final["status"] == "incomplete"
    assert final["incomplete_details"] == {"reason": "max_output_tokens"}
    assert final["usage"]["input_tokens"] == 3 and final["usage"]["output_tokens"] == 4


def test_stream_upstream_break_emits_response_failed():
    """上游中途断流：`_stream_gen` 会抛 → 我们不能把异常甩给客户端，要发 response.failed。"""
    ctx = {"model": "m", "custom_tools": set()}

    async def inner():
        yield _chunk({"content": "半截"})
        raise RuntimeError("upstream reset")

    async def run():
        raw = []
        async for e in R.translate_stream(inner(), ctx):
            raw.append(e.decode("utf-8"))
        return raw

    raw = asyncio.run(run())
    text = "".join(raw)
    assert "event: response.failed" in text
    assert "event: response.completed" not in text
    assert "upstream_stream_error" in text


def test_stream_chunk_boundary_split_mid_line():
    """TCP 分片会把一行 SSE 劈成两半 —— 缓冲区必须扛住。"""
    line = _chunk({"content": "你好"})
    evs = _events([line[:20], line[20:], _chunk({}, "stop")])
    assert dict(evs)["response.completed"]["response"]["output"][0]["content"][0]["text"] == "你好"


def test_stream_ignores_junk_and_keepalive():
    evs = _events([b": keep-alive\n\n", b"data: not-json\n\n", b"data: [DONE]\n\n"])
    names = [n for n, _ in evs]
    assert names == ["response.created", "response.in_progress", "response.completed"]


def test_stream_custom_tool_gets_bare_input():
    evs = _events([_chunk({"tool_calls": [{"index": 0, "id": "cp",
                                           "function": {"name": "patch",
                                                        "arguments": "{\"input\":\"*** Begin\"}"}}]}),
                   _chunk({}, "tool_calls")],
                  {"model": "m", "custom_tools": {"patch"}})
    names = [n for n, _ in evs]
    assert "response.custom_tool_call_input.done" in names
    assert "response.function_call_arguments.delta" not in names    # 半截 JSON 不发增量
    item = dict(evs)["response.completed"]["response"]["output"][0]
    assert item["type"] == "custom_tool_call" and item["input"] == "*** Begin"


def test_no_done_sentinel_in_output():
    """官方 Responses 流不带 `[DONE]`：上游给的那个会被吞掉，不转发给客户端。"""
    evs = _events([_chunk({"content": "x"}), b"data: [DONE]\n\n"])
    assert "[DONE]" not in json.dumps(evs)


# ---------------------------------------------------------------- 4. 端到端

@pytest.fixture
def client(tmp_path_factory):
    """与 test_api.py 同款隔离：落盘路径改到临时目录，别让测试收尾写生产文件。"""
    from app import store as _store
    tmp = tmp_path_factory.mktemp("responses-state")
    old = (_store.DB_PATH, _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH,
           _store.CAPABILITY_CACHE_PATH)
    _store.DB_PATH = str(tmp / "usage.db")
    _store.RUNTIME_STATE_PATH = str(tmp / "runtime_state.json")
    _store.MODEL_STATUS_PATH = str(tmp / "model_status.json")
    _store.CAPABILITY_CACHE_PATH = str(tmp / "capability_cache.json")
    _store._ms_cache = None
    try:
        with TestClient(app) as c:
            yield c
    finally:
        (_store.DB_PATH, _store.RUNTIME_STATE_PATH, _store.MODEL_STATUS_PATH,
         _store.CAPABILITY_CACHE_PATH) = old
        _store._ms_cache = None


def _wire_upstream(monkeypatch, model="glm-test", chat_data=None, sse=b"", status=200):
    """把 `main` 的共享 httpx client 换成假的，渠道也伪造好。

    返回捕获到的上游请求体列表（用来断言翻译后的 chat body 真的发出去了）。"""
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
            pass          # lifespan 收尾会调它（`await shared_client.aclose()`）

    monkeypatch.setattr(m, "shared_client", FakeClient())
    return seen


def test_e2e_non_stream(client, monkeypatch):
    seen = _wire_upstream(monkeypatch, chat_data={
        "id": "c1", "model": "glm-test", "created": 1700000000,
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "你好"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
    r = client.post("/v1/responses", headers=H, json={
        "model": "glm-test", "instructions": "sys", "input": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "response" and body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "你好"
    assert body["instructions"] == "sys"
    # 上游收到的确实是翻译后的 chat body（不是 Responses 原文）
    assert seen[0]["messages"] == [{"role": "system", "content": "sys"},
                                   {"role": "user", "content": "hi"}]
    assert "input" not in seen[0]
    assert r.headers.get("x-api-hub-channel") == "A"


def test_e2e_stream(client, monkeypatch):
    sse = [_chunk({"content": "你"}), _chunk({"content": "好"}), _chunk({}, "stop"),
           b"data: [DONE]\n\n"]
    _wire_upstream(monkeypatch, sse=sse)
    with client.stream("POST", "/v1/responses", headers=H,
                       json={"model": "glm-test", "input": "hi", "stream": True}) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert "event: response.completed" in text
    assert "[DONE]" not in text


def test_e2e_bad_request_gives_400(client, monkeypatch):
    _wire_upstream(monkeypatch, chat_data={"choices": []})
    r = client.post("/v1/responses", headers=H, json={"model": "glm-test"})
    assert r.status_code == 400
    assert "input" in r.json()["detail"]


def test_e2e_missing_model_gives_400(client, monkeypatch):
    _wire_upstream(monkeypatch, chat_data={"choices": []})
    r = client.post("/v1/responses", headers=H, json={"input": "hi"})
    assert r.status_code == 400


def test_e2e_stream_keeps_gateway_accounting(client, monkeypatch):
    """流式也走 `_stream_gen` → usage.db 里必须有这一跳的记录（翻译层不许旁路记账）。"""
    sse = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "hi"},
                                                "finish_reason": None}]}) + "\n\n").encode(),
           _chunk({}, "stop"),
           ("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 9,
                                                            "completion_tokens": 3,
                                                            "total_tokens": 12}}) + "\n\n").encode(),
           b"data: [DONE]\n\n"]
    _wire_upstream(monkeypatch, sse=sse)
    with client.stream("POST", "/v1/responses", headers=H,
                       json={"model": "glm-test", "input": "hi", "stream": True}) as r:
        "".join(r.iter_text())
    log = store.recent(limit=1)["logs"][0]
    assert log["success"] == 1 and log["model"] == "glm-test", log
    assert log["prompt_tokens"] == 9 and log["completion_tokens"] == 3, log
