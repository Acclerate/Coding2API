"""Anthropic 出口：入站请求映射（Messages 请求体 → chat 载荷）。"""

from __future__ import annotations

import pytest

from src.compat.anthropic.request import parse_messages_request
from src.compat.openai.request import InvalidRequest


def _body(**overrides) -> dict:
    body = {"model": "glm-5.2", "max_tokens": 64, "messages": [{"role": "user",
                                                                "content": "hi"}]}
    body.update(overrides)
    return body

def test_parse_requires_json_object():
    with pytest.raises(InvalidRequest, match="JSON object"):
        parse_messages_request([])

def test_parse_requires_model_string():
    with pytest.raises(InvalidRequest, match="model must be a string"):
        parse_messages_request(_body(model=5))

def test_parse_requires_non_empty_messages():
    with pytest.raises(InvalidRequest, match="non-empty array"):
        parse_messages_request(_body(messages=[]))
    with pytest.raises(InvalidRequest, match="non-empty array"):
        parse_messages_request(_body(messages="hi"))

def test_parse_requires_boolean_stream():
    with pytest.raises(InvalidRequest, match="stream must be a boolean"):
        parse_messages_request(_body(stream="yes"))

def test_parse_system_string_and_list():
    system = parse_messages_request(_body(system="sys"))
    assert system.messages[0] == {"role": "system", "content": "sys"}
    blocks = parse_messages_request(_body(system=[
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
    assert blocks.messages[0] == {"role": "system", "content": "ab"}

def test_parse_system_rejects_bad_shapes():
    with pytest.raises(InvalidRequest, match="system\\[0\\] must be an object"):
        parse_messages_request(_body(system=["x"]))
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(system=[{"type": "image"}]))
    with pytest.raises(InvalidRequest, match="text must be a string"):
        parse_messages_request(_body(system=[{"type": "text", "text": 1}]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(system=5))

def test_parse_empty_system_is_dropped():
    parsed = parse_messages_request(_body(system=""))
    assert parsed.messages[0]["role"] == "user"

def test_parse_string_content_by_role():
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]))
    assert [m["role"] for m in parsed.messages] == ["user", "assistant"]

def test_parse_system_role_messages_merge_into_system():
    # 字符串 content
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": "u"},
        {"role": "system", "content": "hook-output"}]))
    assert parsed.messages[0] == {"role": "system", "content": "hook-output"}
    assert [m["role"] for m in parsed.messages[1:]] == ["user"]
    # 数组 content 与顶层 system 合并，按出现顺序拼接
    parsed = parse_messages_request(_body(system="top", messages=[
        {"role": "system", "content": [{"type": "text", "text": "a"},
                                        {"type": "text", "text": "b"}]},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "c"}]))
    assert parsed.messages[0] == {"role": "system", "content": "topabc"}
    assert [m["role"] for m in parsed.messages[1:]] == ["user"]
    # 仅 system 角色消息时也构成合法请求
    parsed = parse_messages_request(_body(
        messages=[{"role": "system", "content": "only"}]))
    assert parsed.messages == [{"role": "system", "content": "only"}]

def test_parse_system_role_message_rejects_bad_blocks():
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(messages=[{"role": "system", "content": [
            {"type": "image"}]}]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "system", "content": 5}]))

def test_parse_rejects_unknown_role_and_bad_message():
    with pytest.raises(InvalidRequest, match="role 'tool' is not supported"):
        parse_messages_request(_body(messages=[{"role": "tool", "content": "x"}]))
    with pytest.raises(InvalidRequest, match=r"messages\[0\] must be an object"):
        parse_messages_request(_body(messages=["x"]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "user", "content": 5}]))

def test_parse_user_blocks_split_text_and_tool_results():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "res"},
        {"type": "text", "text": "again"},
    ]}]))
    assert parsed.messages == [
        {"role": "user", "content": "look"},
        {"role": "tool", "tool_call_id": "t1", "content": "res"},
        {"role": "user", "content": "again"},
    ]

def test_parse_tool_result_text_shapes():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"type": "tool_result", "tool_use_id": "t2"},
    ]}]))
    assert parsed.messages[0]["content"] == "ab"
    assert parsed.messages[1]["content"] == ""

def test_parse_user_blocks_reject_bad_entries():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [1]}]))
    with pytest.raises(InvalidRequest, match="text must be a string"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "text", "text": 1}]}]))
    with pytest.raises(InvalidRequest, match="tool_use_id must be a non-empty"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ""}]}]))
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "image"}]}]))
    with pytest.raises(InvalidRequest, match="unknown content block type"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "bogus"}]}]))

def test_parse_tool_result_content_bad_shapes():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [1]}]}]))
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "image"}]}]}]))
    with pytest.raises(InvalidRequest, match="unknown tool_result.content type"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "bogus"}]}]}]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": 5}]}]))

def test_parse_assistant_blocks_merge_into_one_message():
    parsed = parse_messages_request(_body(messages=[{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "th"},
        {"type": "text", "text": "a"},
        {"type": "redacted_thinking", "data": "x"},
        {"type": "tool_use", "id": "t1", "name": "f", "input": {"x": 1}},
    ]}]))
    assert parsed.messages == [{
        "role": "assistant", "content": "a", "reasoning_content": "th",
        "tool_calls": [{"id": "t1", "type": "function",
                        "function": {"name": "f", "arguments": '{"x": 1}'}}]}]

def test_parse_assistant_empty_is_dropped():
    parsed = parse_messages_request(_body(messages=[
        {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "x"}]},
        {"role": "user", "content": "hi"}]))
    assert parsed.messages == [{"role": "user", "content": "hi"}]

def test_parse_assistant_rejects_bad_blocks():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [1]}]))
    with pytest.raises(InvalidRequest, match="thinking must be a string"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "thinking", "thinking": 1}]}]))
    with pytest.raises(InvalidRequest, match="tool_use.name must be a non-empty"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": ""}]}]))
    with pytest.raises(InvalidRequest, match="unknown content block type"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "bogus"}]}]))

def test_parse_tool_use_input_shapes():
    def args(value):
        return parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "f", "input": value}]}])) \
            .messages[0]["tool_calls"][0]["function"]["arguments"]

    assert args(None) == ""
    assert args({"a": 1}) == '{"a": 1}'
    assert args('{"a":1}') == '{"a":1}'
    with pytest.raises(InvalidRequest, match="input must be an object"):
        args(5)

def test_parse_tools_mapping_and_validation():
    parsed = parse_messages_request(_body(tools=[
        {"name": "f", "description": "d", "input_schema": {"type": "object"}}]))
    assert parsed.raw["tools"] == [{"type": "function", "function": {
        "name": "f", "description": "d", "parameters": {"type": "object"}}}]
    # 无 description / input_schema：只保留 name（不写 None）
    bare = parse_messages_request(_body(tools=[{"name": "f"}]))
    assert bare.raw["tools"][0]["function"] == {"name": "f"}
    assert "description" not in bare.raw["tools"][0]["function"]
    # 空工具列表不写入 upstream
    assert "tools" not in parse_messages_request(_body(tools=[])).raw
    with pytest.raises(InvalidRequest, match="tools must be an array"):
        parse_messages_request(_body(tools="x"))
    with pytest.raises(InvalidRequest, match=r"tools\[0\] must be an object"):
        parse_messages_request(_body(tools=[1]))
    with pytest.raises(InvalidRequest, match=r"tools\[0\].name must be a non-empty"):
        parse_messages_request(_body(tools=[{"name": ""}]))

def test_parse_assistant_reasoning_only_and_tools_only():
    reasoning = parse_messages_request(_body(messages=[{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "th"}]}]))
    assert reasoning.messages[0] == {"role": "assistant", "content": "",
                                     "reasoning_content": "th"}
    tools = parse_messages_request(_body(messages=[{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "f", "input": {}}]}]))
    assert tools.messages[0]["content"] == "" and "reasoning_content" not in tools.messages[0]

def test_parse_drops_all_messages_when_only_redacted_thinking():
    with pytest.raises(InvalidRequest, match="at least one message"):
        parse_messages_request(_body(messages=[
            {"role": "assistant", "content": [{"type": "redacted_thinking"}]},
            {"role": "assistant", "content": [{"type": "redacted_thinking"}]}]))

def test_parse_tool_choice_mapping():
    def choice(value):
        return parse_messages_request(_body(tool_choice=value)).raw["tool_choice"]

    assert choice({"type": "auto"}) == "auto"
    assert choice({"type": "any"}) == "required"
    assert choice({"type": "none"}) == "none"
    assert choice({"type": "tool", "name": "f"}) == {
        "type": "function", "function": {"name": "f"}}
    assert "tool_choice" not in parse_messages_request(_body()).raw

def test_parse_tool_choice_rejects_bad_shapes():
    with pytest.raises(InvalidRequest, match="tool_choice must be an object"):
        parse_messages_request(_body(tool_choice="auto"))
    with pytest.raises(InvalidRequest, match="tool_choice.name must be a non-empty"):
        parse_messages_request(_body(tool_choice={"type": "tool", "name": ""}))
    with pytest.raises(InvalidRequest, match="tool_choice type"):
        parse_messages_request(_body(tool_choice={"type": "bogus"}))

def test_parse_passthrough_fields():
    parsed = parse_messages_request(_body(max_tokens=10, temperature=0.5, top_p=0.9,
                                          stop_sequences=["x"], top_k=5))
    assert parsed.raw["max_tokens"] == 10
    assert parsed.raw["temperature"] == 0.5 and parsed.raw["top_p"] == 0.9
    assert parsed.raw["stop"] == ["x"]
    assert "top_k" not in parsed.raw            # chat 无等价物，丢弃
    assert "max_tokens" not in parse_messages_request(
        _body(max_tokens=None)).raw

def test_parse_rejects_bad_max_tokens_and_stop_sequences():
    with pytest.raises(InvalidRequest, match="max_tokens must be an integer"):
        parse_messages_request(_body(max_tokens="x"))
    with pytest.raises(InvalidRequest, match="max_tokens must be an integer"):
        parse_messages_request(_body(max_tokens=True))
    with pytest.raises(InvalidRequest, match="stop_sequences must be an array"):
        parse_messages_request(_body(stop_sequences="x"))

def test_parse_rejects_out_of_range_and_non_finite_numbers():
    """负数/超大 max_tokens、NaN/Inf 采样参数在入站即 400，不透传给上游。"""
    with pytest.raises(InvalidRequest, match="between 1 and"):
        parse_messages_request(_body(max_tokens=-1))
    with pytest.raises(InvalidRequest, match="between 1 and"):
        parse_messages_request(_body(max_tokens=10**9))
    with pytest.raises(InvalidRequest, match="must be a finite number"):
        parse_messages_request(_body(temperature=float("nan")))
    with pytest.raises(InvalidRequest, match="must be a number"):
        parse_messages_request(_body(top_p="hot"))
    with pytest.raises(InvalidRequest, match="temperature must be <="):
        parse_messages_request(_body(temperature=99999))
    with pytest.raises(InvalidRequest, match="array of strings"):
        parse_messages_request(_body(stop_sequences=["ok", 1]))

def test_parse_arguments_shapes():
    from src.compat.anthropic.response import _parse_arguments

    assert _parse_arguments({"a": 1}) == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments(None) == {}
    assert _parse_arguments("not-json") == {}
    assert _parse_arguments("[1,2]") == {}
