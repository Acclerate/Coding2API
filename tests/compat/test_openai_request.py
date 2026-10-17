"""OpenAI chat 入站请求：`response_format` 语义收口（与 Responses 出口共用）。

此前 chat 入站整包透传，畸形/未知的 `response_format` 会一路打到上游、按 4xx
冷却凭证；这里固定「放行三种形状、其余显式 400、未知字段丢弃」的口径。
"""

from __future__ import annotations

import pytest

from src.compat.openai.request import InvalidRequest, normalize_response_format, parse_chat_request

MESSAGES = [{"role": "user", "content": "hi"}]


def _parse(**overrides) -> dict:
    body = {"messages": MESSAGES, "model": "m"}
    body.update(overrides)
    return parse_chat_request(body).raw


@pytest.mark.parametrize("kind", ["text", "json_object"])
def test_simple_types_are_normalized(kind):
    """`text` / `json_object` 只留 type：多余字段既非规范也无上游认。"""
    raw = _parse(response_format={"type": kind, "name": "noise"})
    assert raw["response_format"] == {"type": kind}

def test_json_schema_keeps_known_fields_only():
    raw = _parse(response_format={
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": {"type": "object"},
                        "description": "d", "strict": True, "bogus": 1},
    })
    assert raw["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": {"type": "object"},
                        "description": "d", "strict": True},
    }

def test_json_schema_optional_fields_are_omitted():
    """`description` / `strict` 可选：非预期类型直接丢，不伪造。"""
    normalized = normalize_response_format({
        "type": "json_schema",
        "json_schema": {"name": "n", "schema": {}, "description": 5, "strict": "yes"},
    })
    assert normalized == {"type": "json_schema",
                          "json_schema": {"name": "n", "schema": {}}}

def test_absent_response_format_stays_absent():
    assert "response_format" not in _parse()
    # 显式 null 原样保留（与其它透传字段一致）：上游按「未指定」处理
    assert _parse(response_format=None)["response_format"] is None

@pytest.mark.parametrize("value, message", [
    ("json", "response_format must be an object"),
    ({"type": "audio"}, "response_format.type 'audio' is not supported"),
    ({"type": "json_schema"}, "response_format.json_schema must be an object"),
    ({"type": "json_schema", "json_schema": []},
     "response_format.json_schema must be an object"),
    ({"type": "json_schema", "json_schema": {"schema": {}}},
     "response_format.name must be a non-empty string"),
    ({"type": "json_schema", "json_schema": {"name": "", "schema": {}}},
     "response_format.name must be a non-empty string"),
    ({"type": "json_schema", "json_schema": {"name": "n"}},
     "response_format.schema is required"),
])
def test_invalid_shapes_raise_400(value, message):
    with pytest.raises(InvalidRequest, match=message):
        _parse(response_format=value)

def test_prefix_only_changes_message_path():
    """Responses 侧传 `text.format` 保持既有报错文案，校验逻辑完全一致。"""
    with pytest.raises(InvalidRequest, match=r"text\.format\.type"):
        normalize_response_format({"type": "nope"}, prefix="text.format")
    assert normalize_response_format({"type": "text"}, prefix="text.format") == {
        "type": "text"}
