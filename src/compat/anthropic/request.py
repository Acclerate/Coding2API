"""Anthropic Messages 请求 → 内部 ChatRequest 的入站映射。

形状取自 Anthropic Messages API（anthropic-python 官方 SDK 类型定义），只覆盖
Claude Code / Anthropic SDK 会用的子集，其余显式 400（`InvalidRequest`），
**不静默降级**。本服务只实现 chat 子集，与 `/v1/chat/completions` 共用同一
执行引擎（选号 / 冷却 / 轮换 / 统计 / 会话粘性全部不动）。

映射要点：
- `system`（字符串或 text 块数组）→ 开头补一条 `system` 消息（空串忽略）
- `messages`：
  - `user` 的 text 块 → user 消息；`tool_result` 块 → `role:"tool"` 消息
    （同一条 user 里 text 与 tool_result 混排时按出现顺序拆分）
  - `assistant` 的 text 块 → assistant 消息正文；`tool_use` 块 → chat
    `tool_calls`（`input` 对象序列化成 `arguments` JSON 串）；`thinking`
    块 → assistant 的 `reasoning_content`
  - `image` 块 → chat 的 `image_url` 内容块（`source.type=base64` 拼成
    data URL，`source.type=url` 直传）；`document` 块仍不支持 → 400
  - 纯文本内容折成字符串（与旧行为逐字节一致），含图片时保留内容块数组
- `tools` 的 `{name, description, input_schema}` → chat
  `{type:"function", function:{name, description, parameters}}`
- `tool_choice`：`auto`→`auto`、`any`→`required`、`tool`→指定函数
- `max_tokens` → chat `max_tokens`（可选；count_tokens 端点不带）
- `stop_sequences` → `stop`；`top_k` 无 chat 等价物，丢弃
"""

from __future__ import annotations

import json
from typing import Any

from ...engine.reasoning import thinking_effort
from ..openai.request import (
    MAX_OUTPUT_TOKENS,
    MAX_TEMPERATURE,
    ChatRequest,
    InvalidRequest,
    require_finite_number,
)


def _text_from_system(system: Any) -> str:
    """`system` 字符串或 text 块数组 → 纯文本；其余形状 400。"""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for index, block in enumerate(system):
            if not isinstance(block, dict):
                raise InvalidRequest(f"system[{index}] must be an object")
            kind = block.get("type")
            if kind != "text":
                raise InvalidRequest(
                    f"system block type {kind!r} is not supported by this gateway")
            text = block.get("text")
            if not isinstance(text, str):
                raise InvalidRequest(f"system[{index}].text must be a string")
            parts.append(text)
        return "".join(parts)
    if system is None:
        return ""
    raise InvalidRequest("system must be a string or an array")


def _block_text(block: dict[str, Any], where: str) -> str:
    text = block.get("text")
    if not isinstance(text, str):
        raise InvalidRequest(f"{where}.text must be a string")
    return text


def _image_part(block: dict[str, Any], where: str) -> dict[str, Any]:
    """Anthropic `image` 块 → chat 的 `image_url` 内容块。

    只做结构映射，不下载 / 不压缩 / 不改写图片字节（上游已实测接受 data URL
    与 http(s) URL，见扫描报告 §4.15 实测表）。
    """
    source = block.get("source")
    if not isinstance(source, dict):
        raise InvalidRequest(f"{where}.source must be an object")
    kind = source.get("type")
    if kind == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type:
            raise InvalidRequest(f"{where}.source.media_type must be a non-empty string")
        if not isinstance(data, str) or not data:
            raise InvalidRequest(f"{where}.source.data must be a non-empty string")
        return {"type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"}}
    if kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise InvalidRequest(f"{where}.source.url must be a non-empty string")
        return {"type": "image_url", "image_url": {"url": url}}
    raise InvalidRequest(f"{where}.source.type {kind!r} is not supported by this gateway")


def _text_part(block: dict[str, Any], where: str) -> dict[str, Any]:
    return {"type": "text", "text": _block_text(block, where)}


def _merge_parts(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """内容块数组 → chat content：纯文本折成字符串，含图片时保留数组。

    纯文本折成字符串是为了与旧行为**逐字节一致**——历史请求（不含图片）经此
    出口映射后必须与改动前完全相同，否则会话粘性指纹与上游前缀缓存都会失效。
    """
    if all(part.get("type") == "text" for part in parts):
        return "".join(part["text"] for part in parts)
    # 必须返回副本：调用方紧接着会 clear() 自己的 pending 列表，直接返回同一
    # 对象会把已存入消息的 content 一并清空（数组 part 场景下表现为 content=[]）。
    return list(parts)


def _tool_result_text(content: Any) -> str | list[dict[str, Any]]:
    """`tool_result.content`：字符串或 text/image 块数组。

    `is_error` 是纯客户端语义，chat 上游没有对应字段，按普通文本回灌。
    纯文本结果仍返回字符串（旧行为不变）；含图片时返回内容块数组——chat 的
    `tool` 消息带 `image_url` 数组已实测被上游接受（2026-10-15）。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                raise InvalidRequest(f"tool_result.content[{index}] must be an object")
            where = f"tool_result.content[{index}]"
            kind = block.get("type")
            if kind == "text":
                parts.append(_text_part(block, where))
            elif kind == "image":
                parts.append(_image_part(block, where))
            elif kind == "document":
                raise InvalidRequest(
                    f"tool_result.content type {kind!r} is not supported by this gateway")
            else:
                raise InvalidRequest(
                    f"unknown tool_result.content type {kind!r}")
        return _merge_parts(parts)
    raise InvalidRequest("tool_result.content must be a string or an array")


def _input_arguments(value: Any) -> str:
    """`tool_use.input`（对象）→ chat `arguments` JSON 字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        # 少数客户端把已序列化的字符串塞进来：原样透传
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    raise InvalidRequest("tool_use.input must be an object")


def _user_messages(blocks: list[Any]) -> list[dict[str, Any]]:
    """user 内容块 → 按顺序拆成 user / tool 消息。

    同一条 user 里 text 与图片相邻时合并进同一个 user 消息（保持图文顺序）；
    遇到 `tool_result` 才 flush——它必须另起一条 `tool` 消息。
    """
    messages: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        # 非空即产出：pending 里可能是纯图片（`_merge_parts` 返回数组，非空）
        if pending:
            messages.append({"role": "user", "content": _merge_parts(pending)})
            pending.clear()

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise InvalidRequest(f"messages.content[{index}] must be an object")
        where = f"messages.content[{index}]"
        kind = block.get("type")
        if kind == "text":
            pending.append(_text_part(block, where))
        elif kind == "image":
            pending.append(_image_part(block, where))
        elif kind == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                raise InvalidRequest(
                    "tool_result.tool_use_id must be a non-empty string")
            flush()
            messages.append({"role": "tool", "tool_call_id": tool_use_id,
                             "content": _tool_result_text(block.get("content"))})
        elif kind == "document":
            raise InvalidRequest(
                f"content block type {kind!r} is not supported by this gateway")
        else:
            raise InvalidRequest(f"unknown content block type {kind!r}")
    flush()
    return messages


def _assistant_message(blocks: list[Any]) -> dict[str, Any] | None:
    """assistant 内容块 → 单条 chat assistant 消息（空则 None）。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise InvalidRequest(f"messages.content[{index}] must be an object")
        kind = block.get("type")
        if kind == "text":
            content_parts.append(_block_text(block, f"messages.content[{index}]"))
        elif kind == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                raise InvalidRequest("thinking.thinking must be a string")
            reasoning_parts.append(thinking)
        elif kind == "redacted_thinking":
            # 加密思考内容对 chat 上游无意义，丢弃（无损：上游不认）
            continue
        elif kind == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                raise InvalidRequest("tool_use.name must be a non-empty string")
            tool_calls.append({
                "id": block.get("id") or "call",
                "type": "function",
                "function": {"name": name,
                             "arguments": _input_arguments(block.get("input"))},
            })
        else:
            raise InvalidRequest(f"unknown content block type {kind!r}")
    if not content_parts and not reasoning_parts and not tool_calls:
        return None
    message: dict[str, Any] = {"role": "assistant",
                               "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _map_messages(raw_messages: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        role = item.get("role")
        content = item.get("content")
        if isinstance(content, str):
            if role not in ("user", "assistant"):
                raise InvalidRequest(f"message role {role!r} is not supported")
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise InvalidRequest(
                f"messages[{index}].content must be a string or an array")
        if role == "user":
            messages.extend(_user_messages(content))
        elif role == "assistant":
            mapped = _assistant_message(content)
            if mapped is not None:
                messages.append(mapped)
        else:
            raise InvalidRequest(f"message role {role!r} is not supported")
    return messages


def _map_tools(tools: Any) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise InvalidRequest(f"tools[{index}] must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest(f"tools[{index}].name must be a non-empty string")
        function: dict[str, Any] = {"name": name}
        if isinstance(tool.get("description"), str):
            function["description"] = tool["description"]
        # Anthropic 用 input_schema，chat 用 parameters（同一份 JSON Schema）
        schema = tool.get("input_schema")
        if isinstance(schema, dict):
            function["parameters"] = schema
        mapped.append({"type": "function", "function": function})
    return mapped


def _map_tool_choice(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRequest("tool_choice must be an object")
    kind = value.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest("tool_choice.name must be a non-empty string")
        return {"type": "function", "function": {"name": name}}
    raise InvalidRequest(f"tool_choice type {kind!r} is not supported")


def parse_messages_request(body: Any, *, thinking_efforts: str = "") -> ChatRequest:
    """Anthropic 请求体 → `ChatRequest`。

    `thinking_efforts` 是「`thinking.budget_tokens` → `reasoning_effort`」的
    热更映射文本（见 `engine/reasoning.py`）；**留空 = 完全不映射**，
    `thinking` 照旧被忽略（现状），不写任何上游字段。
    """
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise InvalidRequest("model must be a string")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InvalidRequest("messages must be a non-empty array")

    messages: list[dict[str, Any]] = []
    system = _text_from_system(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(_map_messages(raw_messages))
    if not messages:
        raise InvalidRequest("messages must contain at least one message")

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("stream must be a boolean")

    upstream: dict[str, Any] = {"model": model or "", "messages": messages,
                                "stream": stream}
    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise InvalidRequest("max_tokens must be an integer")
        if max_tokens <= 0 or max_tokens > MAX_OUTPUT_TOKENS:
            raise InvalidRequest(
                f"max_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
        upstream["max_tokens"] = max_tokens
    for key in ("temperature", "top_p"):
        if key in body:
            value = require_finite_number(body[key], key)
            if key == "temperature" and value > MAX_TEMPERATURE:
                raise InvalidRequest(f"temperature must be <= {MAX_TEMPERATURE}")
            upstream[key] = value
    stop_sequences = body.get("stop_sequences")
    if stop_sequences is not None:
        if not isinstance(stop_sequences, list):
            raise InvalidRequest("stop_sequences must be an array")
        if not all(isinstance(item, str) for item in stop_sequences):
            raise InvalidRequest("stop_sequences must be an array of strings")
        upstream["stop"] = stop_sequences
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise InvalidRequest("tools must be an array")
        mapped = _map_tools(tools)
        if mapped:
            upstream["tools"] = mapped
    choice = _map_tool_choice(body.get("tool_choice"))
    if choice is not None:
        upstream["tool_choice"] = choice
    # thinking → reasoning_effort（默认不映射）：Anthropic 只规定 budget_tokens
    # ≥1024、没给「预算 ↔ 档位」对应表，阈值只能由使用者显式配置；留空时这里
    # 一个字段都不写，`thinking` 与现状一样被忽略
    effort = thinking_effort(thinking_efforts, body.get("thinking"))
    if effort is not None:
        upstream["reasoning_effort"] = effort
    return ChatRequest(model=model or "", messages=messages, stream=stream,
                       raw=upstream)
