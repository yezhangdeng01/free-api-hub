"""兼容垫片：真正的实现已经挪到 `app/dialects/responses.py`。

留这一个模块名，是为了让 `main.py` 和 `tests/test_responses.py` 的 import 一个字都不用改
（对外行为零变化）。新代码请直接 import `app.dialects.responses`（或用 `app.dialects` 注册表）。
"""
from .dialects.responses import (  # noqa: F401
    _PASSTHROUGH_FIELDS,
    _ROLE_MAP,
    _SKIP_ITEM_TYPES,
    _MAX_INT,
    _rid,
    _as_text,
    _text_of_parts,
    _chat_content,
    _append_tool_call,
    _messages_from_input,
    _tool_spec,
    _tools_of,
    _tool_choice_of,
    _response_format_of,
    to_chat,
    _reasoning_text,
    _usage_of,
    _unwrap_custom,
    _fn_item,
    _output_items,
    build_response,
    _sse,
    _StreamState,
    translate_stream,
    _hub_headers,
    _json_body,
    convert,
)
