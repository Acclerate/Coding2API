"""CodeArts 上游客户端：聊天流、模型目录、额度与请求错误。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, Event, EventKind, UpstreamReloginRequired, Usage
from src.provider.codearts import auth as codearts_auth
from src.provider.codearts import dpop
from src.provider.codearts import events as codearts_events
from src.provider.codearts.client import (
    CodeArtsClient,
    UpstreamHTTPError,
    _credit_rate,
    _fill_estimated_credit,
    _flatten_content,
    _legacy_blocks,
    _models_from_items,
    _parse_ratio,
    chat_headers,
    derive_chat_id,
    new_chat_id,
    parse_balance,
    prepare_body,
    request_path,
    sdk_date,
    short_headers,
)
from src.provider.codearts.credential import CodeArtsCredential


def _cred(**over) -> CodeArtsCredential:
    base = {
        "uid": "u1", "user_name": "alice",
        "access_key_id": "AK", "secret_access_key": "SK", "security_token": "TOK",
        "expiration": int(time.time()) + 7200,
    }
    base.update(over)
    return CodeArtsCredential.from_dict(base)

def _jwk() -> dict[str, str]:
    return dpop.new_private_jwk()

def _client(handler, **kw) -> CodeArtsClient:
    transport = httpx.MockTransport(handler)
    kw.setdefault("endpoint", "https://snap.test")
    kw.setdefault("benefit_host", "https://gw.test")
    kw.setdefault("sts_host", "https://sts.test")
    return CodeArtsClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None), **kw)

def _sse(*payloads: object) -> str:
    """CodeArts SSE：逐行 `data:`（真实流每行后有空行，逐行读取两者皆可）。"""
    return "".join(f"data: {json.dumps(p)}\n" for p in payloads)

def _token_response(**over) -> dict:
    payload = {
        "credentials": {"access_key_id": "AK2", "secret_access_key": "SK2",
                        "security_token": "TOK2", "expiration": 1999999999},
        "refresh_token": "rt2", "user_id": "u2", "user_name": "bob",
    }
    payload.update(over)
    return payload

def test_client_sdk_date_and_chat_id():
    assert sdk_date(0) == "19700101T000000Z"
    assert len(sdk_date()) == 16 and sdk_date().endswith("Z")
    assert len(new_chat_id()) == 32
    int(new_chat_id(), 16)

def test_client_derive_chat_id_stable_and_fallback():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert derive_chat_id(payload) == derive_chat_id(payload)
    assert len(derive_chat_id(payload)) == 32
    cyclic: list = []
    cyclic.append(cyclic)                              # 不可序列化 → 退回随机
    assert len(derive_chat_id({"messages": cyclic})) == 32

def test_client_request_path_and_headers():
    assert request_path("https://h/v1/model/builtin?b=2&a=1") == (
        "/v1/model/builtin", "b=2&a=1")
    assert request_path("") == ("/", "")
    assert chat_headers(_cred(), trace_id="t")["x-auth-token"] == "TOK"
    benefit = chat_headers(_cred(), trace_id="t", benefit=True)
    assert benefit[codearts_events.HEADER_MAAS_TYPE] == codearts_events.MAAS_BENEFIT
    assert codearts_events.HEADER_MAAS_TYPE not in chat_headers(_cred(), trace_id="t")
    assert short_headers("PromptCenter")["Agent-Type"] == "PromptCenter"
    assert "Agent-Type" not in short_headers()

def test_client_prepare_body_v2_and_legacy():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5,
                         "unknown": 1}, "m", chat_id="c")
    assert body["stream"] is True and body["model"] == "m"
    assert body["chat_id"] == "c" and body["prompt_cache_key"] == "c"
    assert body["tool_stream"] is True and body["temperature"] == 0.5
    assert "unknown" not in body
    body["messages"][0]["content"] = "changed"          # 深拷贝
    assert messages[0]["content"] == "hi"

    legacy = prepare_body({"messages": messages}, "m", chat_id="c", legacy=True,
                          user_id="u")
    assert legacy["task"] == "chat" and legacy["user_id"] == "u"
    assert legacy["messages"][0] == {"type": "text", "text": "hi"}
    assert "user_id" not in prepare_body({"messages": []}, "m", chat_id="c",
                                         legacy=True)

def test_client_legacy_blocks_and_flatten():
    assert _legacy_blocks("nope") == []
    blocks = _legacy_blocks([
        "junk",
        {"role": "assistant", "content": [{"text": "a"}, {"text": "b"}, "x"]},
        {"role": "user", "content": ""},
        {"role": "user", "content": "hi"},
    ])
    assert blocks == [{"type": "text", "text": "assistant: ab"},
                      {"type": "text", "text": "hi"}]
    assert _flatten_content("s") == "s"
    assert _flatten_content(1) == ""
    assert _flatten_content([None, {"text": 2}, {"text": "z"}]) == "z"
    assert _flatten_content([{"text": ""}]) == ""
    assert _flatten_content(None) == ""

def test_client_models_from_items_and_helpers():
    assert _models_from_items("nope", benefit=False) == []
    # 内置：credit[].ratio_display → credit_rate；福利：不带 credit → None。
    models = _models_from_items([
        "junk", {},
        {"model_id": "a", "model_name": "A", "contextWindow": 8000, "maxTokens": 100,
         "credit": [{"ratio_display": "0.7x", "ratio": "0.05"}]},
        {"modelId": "b", "display_name": "B", "max_input_tokens": 900},
    ], benefit=False)
    assert [m.id for m in models] == ["a", "b"]
    assert models[0].credit_rate == 0.7 and models[0].max_input_tokens == 8000
    assert models[0].max_output_tokens == 100
    assert models[1].credit_rate is None          # 无 credit → 不猜
    benefit_models = _models_from_items([
        {"model_id": "c", "name": "C", "credit": [{"ratio_display": "0.7x"}]}],
        benefit=True)
    assert benefit_models[0].credit_rate is None  # 福利走每日 token 池，不打倍率
    none_models = _models_from_items([
        {"model_id": "c", "name": 5, "context_window": True, "max_tokens": -1}],
        benefit=False)
    assert none_models[0].name == "c" and none_models[0].credit_rate is None
    assert none_models[0].max_input_tokens is None
    assert none_models[0].max_output_tokens is None

def test_client_credit_rate_and_ratio_parsing():
    # 多档取首条；缺 ratio_display 时继续找下一个可解析项
    assert _credit_rate([{"ratio": "0.05"}, {"ratio_display": "0.32x"}]) == 0.32
    assert _credit_rate([{"ratio_display": "0.7x"}]) == 0.7
    assert _credit_rate([1]) is None              # 非 dict 档位跳过
    assert _credit_rate("nope") is None
    assert _credit_rate([]) is None
    # 纯数字 / 无 x 后缀 / 布尔 / 非数字串 / 非法小数
    assert _parse_ratio("0.7") == 0.7
    assert _parse_ratio(0.5) == 0.5
    assert _parse_ratio("1.25x") == 1.25
    assert _parse_ratio(True) is None
    assert _parse_ratio("x0.7") is None
    assert _parse_ratio("0.7.1") is None
    assert _parse_ratio(None) is None

def test_client_fill_estimated_credit_benefit_and_guards():
    # 福利模型：输入 + 输出 token 折成积分（1 积分 = 10000 token）后标推算值
    event = Event(kind=EventKind.USAGE, usage=Usage(input_tokens=32, output_tokens=694))
    _fill_estimated_credit(event, benefit=True)
    assert event.usage.credit == pytest.approx(726 / 10000)
    assert event.usage.credit_estimated is True
    # 上游将来真回传 credit → 不覆盖
    upstream = Event(kind=EventKind.USAGE,
                     usage=Usage(input_tokens=1, output_tokens=2, credit=9.5))
    _fill_estimated_credit(upstream, benefit=True)
    assert upstream.usage.credit == 9.5 and upstream.usage.credit_estimated is False
    # 内置模型不扣每日池 → 不补
    builtin = Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1, output_tokens=2))
    _fill_estimated_credit(builtin, benefit=False)
    assert builtin.usage.credit is None and builtin.usage.credit_estimated is False
    # 非 USAGE 事件 / 无 usage / 两个 token 都缺 → 都不猜
    content = Event(kind=EventKind.CONTENT, content="x")
    _fill_estimated_credit(content, benefit=True)
    assert content.usage is None
    _fill_estimated_credit(Event(kind=EventKind.USAGE), benefit=True)
    empty = Event(kind=EventKind.USAGE, usage=Usage())
    _fill_estimated_credit(empty, benefit=True)
    assert empty.usage.credit is None
    # 只给一半 token 也照算（另一侧按 0）
    half = Event(kind=EventKind.USAGE, usage=Usage(output_tokens=5))
    _fill_estimated_credit(half, benefit=True)
    assert half.usage.credit == pytest.approx(5 / 10000)
    assert half.usage.credit_estimated is True

def test_client_parse_balance_variants():
    # 上游是 token、本服务折成积分（1 积分 = 10000 token）：余额/额度/阶梯同口径
    assert parse_balance({}).probe_failed is True
    direct = parse_balance({"total": 100, "used": 30})
    assert direct.remaining == pytest.approx(70 / 10000)
    assert direct.total == pytest.approx(100 / 10000)
    envelope = parse_balance({"result": {"remaining": 5, "total": 10,
                                         "cycle_end": 1700000000}})
    assert envelope.remaining == pytest.approx(5 / 10000)
    assert envelope.cycle_end == 1700000000
    # bool 不算数字；残缺键回落下一个候选
    tolerant = parse_balance({"remaining": True, "balance": 3, "cycle_end": -1,
                              "expire_time": 1700000001})
    assert tolerant.remaining == pytest.approx(3 / 10000)
    assert tolerant.cycle_end == 1700000001
    assert parse_balance({"quota": 0}).probe_failed is False
    # 真实每日池形状（2026-09-30 实测）：按 daily_token_limit 算当日剩余，再折积分
    noon = 1_780_300_800  # 任意时刻；到期点断言不依赖具体时区
    daily = parse_balance({"result": {
        "total_quota": 10000000, "total_balance": 9998868, "used_amount": 1132,
        "daily_token_limit": 10000000, "daily_tokens_used": 1132,
        "monthly_token_limit": 0, "expire_time": 0}}, now=noon)
    # 每日池满额 1000 万 token ≡ 1000 积分；剩余 9998868 token ≡ 999.8868 积分
    assert daily.total == pytest.approx(1000.0)
    assert daily.remaining == pytest.approx(999.8868)
    assert daily.probe_failed is False
    assert daily.probed_at == noon
    # 到期点＝下一个本地 0 点，当日剩余进到期阶梯 → 调度器「快过期的先用」优先消耗
    midnight = time.localtime(daily.cycle_end)
    assert (midnight.tm_hour, midnight.tm_min, midnight.tm_sec) == (0, 0, 0)
    assert noon < daily.cycle_end <= noon + 86400
    assert daily.expiry_ladder == [(daily.cycle_end, pytest.approx(999.8868))]
    # 当日用尽 → 剩余 0（不是负数），健康度会判「已耗尽」；空池不进到期排序
    drained = parse_balance({"daily_token_limit": 100,
                             "daily_tokens_used": 250}, now=noon)
    assert drained.remaining == 0 and drained.total == pytest.approx(0.01)
    assert drained.cycle_end == daily.cycle_end and drained.expiry_ladder is None
    # daily_token_limit 为 0/缺失时退化到通用键（含新补的 total_balance/used_amount）
    generic = parse_balance({"total_quota": 500, "used_amount": 40}, now=noon)
    assert generic.total == pytest.approx(500 / 10000)
    assert generic.remaining == pytest.approx(460 / 10000)
    # 通用形状没有可靠的每日重置时间 → 不登记到期阶梯，也不伪造 cycle_end
    assert generic.expiry_ladder is None and generic.cycle_end is None
    fallback = parse_balance({"result": {"total_balance": 900, "total_quota": 1000}})
    assert fallback.total == pytest.approx(0.1)
    assert fallback.remaining == pytest.approx(0.09)
    # daily_tokens_used 缺失时按 0 处理 → 剩余等于额度
    assert parse_balance({"daily_token_limit": 10},
                         now=noon).remaining == pytest.approx(10 / 10000)

def test_client_upstream_error_kind():
    error = UpstreamHTTPError(401, b"")
    assert error.kind() is ErrKind.DEAD

def test_client_defaults_and_chat_url():
    client = CodeArtsClient(endpoint="https://snap.test/", sts_host="https://sts.test/")
    assert client.endpoint == "https://snap.test"
    assert client.sts_host == "https://sts.test"
    assert client.chat_url() == "https://snap.test" + codearts_events.EP_CHAT_V2
    assert client.is_benefit_model(_cred(), "deepseek-v4-flash-0731") is True
    assert client.is_benefit_model(_cred(), "unknown-model") is False
    legacy = CodeArtsClient(endpoint="https://snap.test", legacy=True,
                            login=codearts_auth.LoginConfig())
    assert legacy.chat_url() == "https://snap.test" + codearts_events.EP_CHAT
    assert legacy.login.client_id == codearts_events.CLIENT_ID

async def test_client_lazy_pools_and_aclose():
    client = CodeArtsClient(endpoint="https://snap.test")
    assert client._stream() is client._stream()          # noqa: SLF001
    assert client._short() is client._short()            # noqa: SLF001
    await client.aclose()
    await CodeArtsClient().aclose()                      # 两侧都没创建 → 不报错

async def test_client_stream_chat_openai_chunks_and_done():
    """v2 实测形状：标准 OpenAI chunk + `data:[DONE]`。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_CHAT_V2
        assert request.url.host == "snap.test"
        body = json.loads(request.content)
        assert body["stream"] is True and body["messages"][0]["content"] == "x"
        assert body["tool_stream"] is True
        assert request.headers["Authorization"].startswith("SDK-HMAC-SHA256 ")
        assert codearts_events.HEADER_MAAS_TYPE not in request.headers
        return httpx.Response(200, text=_sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant",
                                                "reasoning_content": ""},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {"content": "",
                                                "reasoning_content": "想"},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {"content": "好"},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 35, "completion_tokens": 45}},
        ) + "data:[DONE]\n")

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert [e.content for e in events if e.kind is EventKind.CONTENT] == ["好"]
    assert [e.content for e in events if e.kind is EventKind.REASONING] == ["想"]
    assert events[-2].kind is EventKind.USAGE
    assert events[-2].usage.input_tokens == 35
    assert events[-2].usage.output_tokens == 45

async def test_client_stream_chat_snapshot_semantics_and_done():
    """旧形状（累计 `text`）仍兼容：快照做差 + `{"text":"[DONE]"}` 结束。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            {"text": "He"},                                  # 增量 He
            {"text": "Hello"},                               # 前缀 → 增量 llo
            {"text": "Hello"},                               # 无新内容 → 不产事件
            {"text": "Hi"},                                  # 非前缀 → 整段重发
            {"text": "[DONE]", "error_code": "0"},           # 正常结束
        ))

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert [e.content for e in events if e.kind is EventKind.CONTENT] == [
        "He", "llo", "Hi"]
    assert [e.kind for e in events][-1] is EventKind.FINISH

async def test_client_stream_chat_benefit_header_and_embedded_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers[codearts_events.HEADER_MAAS_TYPE] == (
            codearts_events.MAAS_BENEFIT)
        return httpx.Response(200, text=_sse(
            {"text": "[DONE]", "error_code": "ChatAgent.00001001",
             "error_msg": "busy"}))

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": []}, "deepseek-v4-flash-0731")]
    assert len(events) == 1 and events[0].kind is EventKind.ERROR
    assert events[0].error_message == "ChatAgent.00001001 busy"
    assert events[0].error_kind is ErrKind.OTHER

async def test_client_stream_chat_benefit_usage_records_pool_credit():
    """福利模型收尾帧的 usage → 补推算扣池额度（token 1:1 后折积分），标 estimated。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers[codearts_events.HEADER_MAAS_TYPE] == (
            codearts_events.MAAS_BENEFIT)
        return httpx.Response(200, text=_sse(
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 32, "completion_tokens": 694}},
        ) + "data:[DONE]\n")

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]},
        "deepseek-v4-flash-0731")]
    usage = next(e for e in events if e.kind is EventKind.USAGE).usage
    assert usage.input_tokens == 32 and usage.output_tokens == 694
    assert usage.credit == pytest.approx(726 / 10000)
    assert usage.credit_estimated is True

async def test_client_stream_chat_legacy_and_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_CHAT
        body = json.loads(request.content)
        assert body["task"] == "chat"
        assert body["user_id"] == "alice"
        assert codearts_events.HEADER_MAAS_TYPE not in request.headers
        return httpx.Response(200, text=_sse({"text": "[DONE]"}))

    events = [e async for e in _client(handler, legacy=True).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert events[-1].kind is EventKind.FINISH

    def error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(error_handler).stream_chat(_cred(), {}, "m")]
    assert caught.value.kind() is ErrKind.OTHER

async def test_client_fetch_models_merges_builtin_and_benefit():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            assert request.method == "POST"
            return httpx.Response(200, json={"result": "ok"})     # 幂等
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            assert request.headers["Agent-Type"] == "PromptCenter"
            return httpx.Response(200, json={"builtinModels": [
                "junk",
                {"model_id": "m1", "model_name": "M1"},
                {"name": "m2"},
            ]})
        return httpx.Response(200, json={"result": {"models": [
            {"modelId": "b1", "name": "B1"},
            {"model_name": "m1"},                                  # 已转正 → 不进福利目录
            {"id": 7},                                             # 坏条目跳过
        ]}})

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["m1", "m2", "b1"]
    assert "gw.test" + codearts_events.EP_BENEFIT_CLAIM in calls
    # 目录只含未转正的福利模型：m1 有 benefit 头被摘掉，b1 需要
    assert client.is_benefit_model(_cred(), "b1") is True
    assert client.is_benefit_model(_cred(), "m1") is False
    assert client.is_benefit_model(_cred(), "deepseek-v4-pro-0813") is False

async def test_client_fetch_models_partial_failure_keeps_previous_catalog():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(200, json={"builtinModels": [{"id": "m1"}]})
        if request.url.path == codearts_events.EP_BENEFIT_CONFIG:
            return httpx.Response(500, content=b"gw down")
        return httpx.Response(200, json={})                        # claim

    client = _client(handler, benefit_auto_claim=False)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["m1"]
    # 福利来源失败 → 目录保留（不误摘 benefit 头）
    assert client.is_benefit_model(_cred(), "glm-5.3-flash") is True

async def test_client_fetch_models_builtin_failure_uses_benefit_and_empty_catalog():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(500, content=b"builtin down")
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(500, content=b"claim down")       # 静默失败
        return httpx.Response(200, json={"result": {"models": [{"id": "b1"}]}})

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["b1"]
    # 福利来源成功但为空集合 → 整体替换（非种子模型不必带 benefit 头）
    assert client.is_benefit_model(_cred(), "deepseek-v4-flash-0731") is False

async def test_client_fetch_models_empty_raises_violation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})                         # 两路都无模型

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="empty list"):
        await _client(handler).fetch_models(_cred())

async def test_client_claim_benefit_and_quota_and_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(200, json={"result": {"claimed": True}})
        if request.url.path == codearts_events.EP_TOKEN_BALANCE:
            return httpx.Response(200, json={"result": {"remaining": 42, "total": 100}})
        if request.url.path == codearts_events.EP_CALLER_IDENTITY:
            return httpx.Response(200, json={
                "principal_id": "pid", "account_id": "acc",
                "principal_urn": "urn:...:user:carol"})
        if request.url.path == "/snap-manager" + codearts_events.EP_CURRENT_USER:
            return httpx.Response(200, json={
                "user_id": "u", "user_name": "n", "domain_id": "d"})
        return httpx.Response(500, content=b"unexpected")

    client = _client(handler)
    assert (await client.claim_benefit(_cred()))["result"]["claimed"] is True
    quota = await client.probe_quota(_cred())
    assert quota.remaining == pytest.approx(42 / 10000)
    assert quota.total == pytest.approx(100 / 10000)
    assert await client.caller_identity(_cred()) == ("pid", "carol", "acc")
    assert await client.current_user(_cred()) == ("u", "n", "d")

    def urnless(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"principal_id": "p", "account_id": "a"})

    assert await _client(urnless).caller_identity(_cred()) == ("p", "", "a")

async def test_client_probe_quota_never_refreshes_even_in_window():
    """额度探测只读余额：即使凭证已在刷新窗口，也不得消费一次性 refresh_token。

    刷新唯一归 `RefreshTask`（先落库再同步）。这里若刷新，会与 RefreshTask 抢
    同一个一次性 token，后到的报 `the refresh token has been used` 且本处结果
    不落库——实测由此把渠道打成硬失效。
    """
    jwk = _jwk()
    sts_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sts_calls
        if request.url.host == "sts.test":
            sts_calls += 1
            return httpx.Response(200, json=_token_response())
        return httpx.Response(200, json={"remaining": 7})

    client = _client(handler)
    expiring = _cred(refresh_token="rt", dpop_private_jwk=jwk,
                     expiration=int(time.time()) + 5)
    quota = await client.probe_quota(expiring)
    assert sts_calls == 0 and quota.remaining == pytest.approx(7 / 10000)

async def test_client_refresh_token_rotates_via_sts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sts.test"
        assert request.headers["DPoP"]
        form = httpx.QueryParams(request.content.decode())
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "rt-old"
        return httpx.Response(200, json={
            "credentials": {"access_key_id": "AKN", "secret_access_key": "SKN",
                            "security_token": "TOKN"},
            "refresh_token": "rt-new", "user_id": "u1"})

    refreshed = await _client(handler).refresh_token(
        _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))
    assert refreshed.refresh_token == "rt-new"              # 必须轮转回写
    assert refreshed.access_key_id == "AKN"

async def test_client_refresh_token_spent_raises_relogin_required():
    """一次性票被消费 → 终态异常（不是可重试失败，RefreshTask 据此停手）。"""
    def burned(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=(
            b'{"error_code":"STS5.1806","error_msg":"invalid refresh token: '
            b"\'the refresh token has been used\'\"}"))

    with pytest.raises(UpstreamReloginRequired, match="需重新登录"):
        await _client(burned).refresh_token(
            _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))

async def test_client_refresh_token_retryable_failure_not_terminal():
    """5xx / 普通 4xx 是可重试的失败：原样上抛，不能升级成终态。"""
    def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream busy")

    with pytest.raises(codearts_auth.TokenEndpointError):
        await _client(server_error).refresh_token(
            _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))

async def test_client_request_json_error_paths():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).caller_identity(_cred())

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="non-JSON"):
        await _client(non_json).caller_identity(_cred())

    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="shape"):
        await _client(non_object).caller_identity(_cred())
