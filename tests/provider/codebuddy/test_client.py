"""CodeBuddy 上游客户端：聊天流、额度探测、动态模型列表与错误分类。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.codebuddy import events as cb_events
from src.provider.codebuddy.client import (
    DEFAULT_MODELS,
    CodeBuddyClient,
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
    _cycle_end_epoch,
)
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.headers import QUOTA_RANGE_END
from tests.helpers.paths import provider_fixture


def _quota_body(accounts: object) -> dict:
    """包出上游真实的个人版额度响应结构（data.Response.Data.Accounts）。"""
    return {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": accounts},
                                  "RequestId": "<redacted>"}}}

def fixture(name: str) -> str:
    return provider_fixture("codebuddy", name)

@pytest.fixture(autouse=True)
def _clear_model_cache():
    """模块级模型缓存必须按测试清空，否则用例间互相污染。"""
    from src.provider.codebuddy.client import _MODEL_CACHE

    _MODEL_CACHE.clear()
    # `_days()` 的基准时刻同理：按测试重置，避免整套跑时基准越来越旧
    global _DAYS_BASE
    _DAYS_BASE = None
    yield
    _MODEL_CACHE.clear()

def _client(handler) -> CodeBuddyClient:
    transport = httpx.MockTransport(handler)
    return CodeBuddyClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None))

def _days(offset: int) -> str:
    """相对基准时刻的偏移，返回上游格式的时间串（有符号，负数=过去）。

    基准时刻在**首次调用时固定**（`_days` 会被调用多次：构造 payload 一次、
    断言里再算一次）。若每次现取 `time.time()`，两者跨过整秒就会差 1 秒，
    `quota.expiry_ladder == [(_cycle_end_epoch(_days(3)), 40.0)]` 这类等值
    断言会随机翻红——带覆盖率跑（更慢）时尤其容易命中。
    偏移量都是小时级，基准固定后与真实 now 的漂移可忽略。
    """
    global _DAYS_BASE
    if _DAYS_BASE is None:
        _DAYS_BASE = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_DAYS_BASE + offset * 3600))

@pytest.mark.parametrize(("status", "expected"), [
    (401, ErrKind.DEAD), (403, ErrKind.DEAD), (404, ErrKind.SOFT), (429, ErrKind.SOFT),
    (500, ErrKind.OTHER), (400, ErrKind.INVALID), (200, ErrKind.OTHER),
    (402, ErrKind.CREDIT),
])
def test_classify_status(status, expected):
    assert cb_events.classify_status(status) is expected

def test_classify_body_markers():
    assert cb_events.classify_status(400, fixture("error-1005.json").encode()) is ErrKind.PLAN
    assert cb_events.classify_status(400, b'{"code": 1005, "plan": "x"}') is ErrKind.PLAN
    assert cb_events.classify_status(401, fixture("error-401.json").encode()) is ErrKind.DEAD

@pytest.mark.parametrize(("status", "body", "expected"), [
    # 余额不足：402 或 body 里的 14018（credits exhausted）
    (402, b'{"code": 14018, "msg": "credits exhausted"}', ErrKind.CREDIT),
    (429, b'{"code": 14018}', ErrKind.CREDIT),
    # 权益耗尽（1005）与余额不足是两种语义：前者 12h，后者等签到
    (400, b'{"code": 1005}', ErrKind.PLAN),
    # 模型级限流：429 + 6004 只冷却触发模型，不是账号级 SOFT
    (429, b'{"code": 6004, "msg": "model quota exceeded"}', ErrKind.MODEL),
    # 「该后端无此模型」：(账号, 模型) 负缓存
    (400, b'{"code": 11102}', ErrKind.BLOCKED),
    (404, b'{"code": 11102}', ErrKind.BLOCKED),
    # 请求级错误：不罚号，仍换号（请求体坏 / 上下文超限 / 渠道风控 / 图片无效）
    (400, b'{"code": 11101}', ErrKind.REQUEST),
    (400, b'{"code": 500, "msg": "Unmarshal chat params failed"}', ErrKind.REQUEST),
    (400, b'{"code": 11115, "msg": "prompt is too long"}', ErrKind.REQUEST),
    # 渠道风控（瞬时，窗口内自愈）：曾误落 INVALID → 零重试直接 400，实测误伤
    (400, b'{"code": 11128, "msg": "Illegal API invocation from an unapproved channel"}',
     ErrKind.REQUEST),
    (400, b'{"code": 11135, "msg": "Invalid image data"}', ErrKind.REQUEST),
    # 裸数字不误伤：111020 不是 11102（要求带 "code": 键值形态）
    (400, b'{"trace": 1110201}', ErrKind.INVALID),
    (400, b'{"code": 111020}', ErrKind.INVALID),
    # 嵌套信封：业务码在内层，按集合成员判断不漏
    (400, b'{"code": 0, "data": {"code": 11102}}', ErrKind.BLOCKED),
    (404, b"not json at all", ErrKind.SOFT),
    (500, b'{"code": 9999}', ErrKind.OTHER),
])
def test_classify_status_business_codes(status, body, expected):
    assert cb_events.classify_status(status, body) is expected

async def test_stream_chat_yields_events_from_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/chat/completions"
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    events = [e async for e in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"messages": []}, "glm-5.2")]
    # 收尾帧同时带 finish_reason 与 usage → 两个事件都产出
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT,
                                        EventKind.USAGE, EventKind.FINISH]

async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), {}, "m")]
    assert caught.value.kind() is ErrKind.SOFT

async def test_stream_chat_forces_stream_true_even_if_client_sent_false():
    """上游只有流式；客户端 stream=false 也必须强制 true（AGENTS.md 约束）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    [e async for e in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"stream": False}, "glm-5.2")]
    assert seen["stream"] is True and seen["model"] == "glm-5.2"

async def test_fetch_personal_quota_parses_real_nested_response():
    """真实响应是 data.Response.Data.Accounts，且 *Precise 是字符串。

    用抓取的真实结构做契约测试：层级读错或忽略字符串都会让有效凭证
    被误判成「未探测到额度」。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    # fixture 是脱敏后的真实响应：两个 Status=0 的套餐（累加 total 5500），
    # remaining 取逐包 CycleCapacityRemainPrecise 之和（500 + 4767.50000158）。
    # 响应顶层另有 TotalDosage=9070，**必须忽略**：它大于按 size 累加的 total，
    # 是「上周期」口径、不随本周期消耗变化，采用它会让 remaining == total、
    # 健康度恒 100（2026-09-28 线上事故）。
    assert quota.total == 5500.0
    assert quota.remaining == pytest.approx(5267.50000158)
    assert quota.cycle_end is not None
    assert quota.probe_failed is False

async def test_total_dosage_is_ignored_in_favor_of_per_package_remaining():
    """回归：响应里的 TotalDosage 是陈旧的上周期口径，绝不能覆盖逐包剩余。

    构造一个 TotalDosage 与逐包和明显不同的响应，断言 remaining 取逐包口径。
    """
    body = {"code": 0, "msg": "OK", "data": {"Response": {"Data": {
        "TotalDosage": 9070,
        "Accounts": [
            {"Status": 0, "CycleCapacitySizePrecise": "500",
             "CycleCapacityRemainPrecise": "480"},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "60"},
        ],
    }}}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 600.0
    assert quota.remaining == 540.0            # 480 + 60，而非 TotalDosage=9070
    assert quota.remaining < quota.total       # 消耗可见，健康度不再恒 100

async def test_fetch_personal_quota_prefers_precise_over_plain():
    """Precise 优先，且能解析字符串；被禁用的套餐（Status!=0）要跳过。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "60", "CycleCapacitySize": 999,
             "CycleEndTime": "2026-12-31 23:59:59"},
            {"Status": 0, "CycleCapacitySize": 50, "CycleCapacityRemain": 10},
            {"Status": 1, "CycleCapacitySizePrecise": "777",
             "CycleCapacityRemainPrecise": "777"},
            "junk",
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 150.0      # 100(Precise 字符串) + 50(回退非 Precise)
    assert quota.remaining == 70.0
    assert quota.cycle_end is not None

async def test_fetch_personal_quota_cycle_end_is_earliest_not_expired():
    """账号常有几十个套餐各自独立到期（每日 100 积分 × N），cycle_end 取最早的那个。

    上游会把已过期套餐一起返回（EndTimeRangeBegin 过滤的是套餐有效期，不是积分周期），
    直接取最小值会得到过去的时间，调度器的到期偏好就永远不触发。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(400)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(2)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "64", "CycleEndTime": _days(-1)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end == _cycle_end_epoch(_days(2))
    assert quota.cycle_end < _cycle_end_epoch(_days(400))
    assert quota.cycle_end > time.time()          # 已过期套餐不参与
    assert quota.total == 300.0
    # 到期阶梯保留套餐顺序，只收未过期的包
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(400)), 100.0),
                                   (_cycle_end_epoch(_days(2)), 100.0)]

async def test_fetch_personal_quota_expiry_ladder_filters_packages():
    """到期阶梯只收「未过期 + 有余额」的包：状态异常、零容量、已用完、
    时间格式不合法的套餐都排除；total/remaining 仍按原口径累加。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 1, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "0",
             "CycleCapacityRemainPrecise": "0", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "0", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": "not-a-date"},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "40", "CycleEndTime": _days(3)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(3)), 40.0)]
    assert quota.total == 300.0
    assert quota.remaining == 140.0
    assert quota.cycle_end == _cycle_end_epoch(_days(3))

async def test_fetch_personal_quota_packages_include_names_and_usage():
    """展示明细比调度阶梯宽：已用完但仍有效的包要显示（管理员想知道哪包空了），
    但「已过期且已用完」的历史残留不堆进列表。与 ladder 互不影响。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            # 未过期 + 有余额 → 进明细，也进阶梯
            {"Status": 0, "PackageName": "CodeBuddy个人体验版",
             "CycleCapacitySizePrecise": "500", "CycleCapacityRemainPrecise": "100",
             "CycleEndTime": _days(2)},
            # 未过期但已用完 → 只进明细（展示“已用完”），不进阶梯
            {"Status": 0, "PackageName": "已用完的包",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "0",
             "CycleEndTime": _days(3)},
            # 已过期但还有余额 → 只进明细（展示“浪费了”），不进阶梯
            {"Status": 0, "PackageName": "已过期有余额",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "50",
             "CycleEndTime": _days(-1)},
            # 已过期 + 已用完 → 两边都不进（历史残留）
            {"Status": 0, "PackageName": "历史残留",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "0",
             "CycleEndTime": _days(-2)},
            # 无到期时间但还有余额 → 进明细，end 为 None
            {"Status": 0, "PackageName": "无到期",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "10"},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert [item["name"] for item in quota.packages] == [
        "CodeBuddy个人体验版", "已用完的包", "已过期有余额", "无到期"]
    assert quota.packages[0] == {"name": "CodeBuddy个人体验版", "total": 500.0,
                                 "used": 400.0, "end": _cycle_end_epoch(_days(2))}
    assert quota.packages[1]["used"] == 100.0      # 已用完
    assert quota.packages[3]["end"] is None        # 无到期信息
    # 调度阶梯不受展示口径影响：已过期的包不进阶梯（只收未过期 + 有余额）
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(2)), 100.0)]

async def test_fetch_personal_quota_cycle_end_none_when_all_packages_expired():
    """套餐全部过期（积分已作废）时不给出过去的到期点。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(-2)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(-48)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None
    assert quota.total == 200.0
    assert quota.expiry_ladder == []

@pytest.mark.parametrize("accounts", [None, []])
async def test_fetch_quota_accepts_null_accounts_as_no_personal_quota(accounts):
    """个人版 Accounts: null / [] 表示探测成功但没有额度（AGENTS.md 约束）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 0 and quota.remaining == 0
    assert quota.probe_failed is False

async def test_fetch_personal_quota_omits_product_code():
    """回归：探测请求不带 ProductCode。

    上游对部分账号已拒绝 "codebuddy" 产品码（InvalidParameterValue:
    productCode:param format error），缺省时才返回全部套餐。
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=_quota_body([]))

    await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert bodies == [{"PageNumber": 1, "PageSize": 200, "Status": [0, 3],
                       "PackageEndTimeRangeBegin": bodies[0]["PackageEndTimeRangeBegin"],
                       "PackageEndTimeRangeEnd": QUOTA_RANGE_END}]

@pytest.mark.parametrize("message", ["[productCode:param format error] ", None])
async def test_fetch_quota_response_error_is_probe_failure(message):
    """上游业务层错误（Response.Error）必须抛探测失败，不能当成 0 额度
    把还有积分的凭证误标成「已耗尽」。"""
    error = {"Code": "InvalidParameterValue", "Message": message}
    body = {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": None,
                                          "TotalDosage": 0},
                                  "Error": error}}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation, match="InvalidParameterValue"):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))

@pytest.mark.parametrize("body", [
    {},                                     # 完全没有 data
    {"data": None},
    {"data": {}},                           # data 里没有 Accounts
    {"data": {"Response": {}}},
    {"data": {"Response": {"Data": {}}}},
    {"data": {"Accounts": "no"}},           # 类型错误
])
async def test_fetch_quota_missing_accounts_fails(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))

async def test_fetch_enterprise_quota_requires_oauth():
    """企业额度接口只对 OAuth 凭证开放；手动凭证走个人版。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("get-enterprise-user-usage"):
            return httpx.Response(200, json={"credit": 30, "limitNum": 100})
        return httpx.Response(200, json=_quota_body([]))

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                enterprise_id="e", quota_probe_mode="enterprise")
    quota = await _client(handler).fetch_quota(oauth)
    assert quota.total == 100 and quota.remaining == 70
    assert calls[-1].endswith("get-enterprise-user-usage")

    manual = CodeBuddyCredential(bearer_token="t", quota_probe_mode="enterprise")
    await _client(handler).fetch_quota(manual)
    assert calls[-1].endswith("get-user-resource")

async def test_fetch_enterprise_quota_rejects_missing_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credit": 5})

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                quota_probe_mode="enterprise")
    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(oauth)

async def test_post_json_error_paths():
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await _client(failing).fetch_quota(CodeBuddyCredential(bearer_token="t"))

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(not_json).fetch_quota(CodeBuddyCredential(bearer_token="t"))

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(not_object).fetch_quota(CodeBuddyCredential(bearer_token="t"))

async def test_client_lazy_properties_and_close():
    client = CodeBuddyClient()
    assert client._stream is client._stream
    assert client._short is client._short
    await client.aclose()
    await client.aclose()

def test_provider_import_classify_and_models():
    provider = CodeBuddyProvider()
    data = provider.import_credential({"token": "abc"})
    assert data["bearer_token"] == "abc" and data["auth_source"] == "manual"
    assert provider.classify(429, b"") is ErrKind.SOFT
    assert provider.host() == "copilot.tencent.com"
    with pytest.raises(UpstreamProtocolViolation):
        provider.import_credential({})

async def test_provider_stream_and_probe_delegate():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(handler))
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert events[-1].kind is EventKind.FINISH
    assert any(e.kind is EventKind.USAGE for e in events)

async def test_fetch_quota_skips_packages_with_zero_total():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "0", "CycleCapacityRemainPrecise": "5"},
            {"Status": 0, "CycleCapacitySizePrecise": "10",
             "CycleCapacityRemainPrecise": "10"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 10 and quota.remaining == 10

async def test_fetch_quota_tolerates_bad_cycle_end_format():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "5",
             "CycleCapacityRemainPrecise": "5", "CycleEndTime": "not-a-date"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None

def test_cycle_end_accepts_iso_separator():
    from src.provider.codebuddy.client import _cycle_end_epoch

    assert _cycle_end_epoch("2026-12-31T23:59:59") is not None
    assert _cycle_end_epoch(None) is None
    assert _cycle_end_epoch("") is None

async def test_provider_probe_quota_delegates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    provider = CodeBuddyProvider(client=_client(handler))
    quota = await provider.probe_quota({"bearer_token": "t"})
    assert quota.total == 5500.0

async def test_aclose_when_only_short_client_was_created():
    """只创建 short 客户端时，aclose 跳过 stream（client 159→158）。"""
    client = CodeBuddyClient()
    assert client._short is not None    # 只实例化 short
    await client.aclose()

@pytest.mark.parametrize(("value", "expected"), [
    (500, 500.0),
    (500.5, 500.5),
    ("500", 500.0),          # 上游 Precise 字段是字符串
    (" 4767.50000158 ", 4767.50000158),
    ("", None),              # 空字符串
    ("   ", None),
    ("not-a-number", None),  # 无法解析
    (None, None),
    ([], None),
    (True, None),            # 布尔不算数字
    (False, None),
])
def test_number_parses_strings_and_rejects_junk(value, expected):
    """额度解析必须能处理字符串型 Precise，否则整批额度会被算成 0。"""
    from src.provider.codebuddy.client import _number

    result = _number(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)

async def test_fetch_models_parses_config_and_caches():
    """/v3/config 动态拉取：data.models[].id，按 token+endpoint 缓存。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}, {"id": "glm-5.3"}, {"id": ""}, "junk",
            {"id": "glm-5.2"},
        ]}})

    client = _client(handler)
    models = await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert [m.id for m in models] == ["glm-5.2", "glm-5.3"]
    await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert calls["n"] == 1                      # 命中缓存

async def test_fetch_models_cache_is_bounded():
    """模型缓存有容量上限：先淘汰过期条目，仍满则整体清空（LOW）。"""
    from src.provider.codebuddy.client import (
        _MODEL_CACHE,
        MODEL_CACHE_MAX_ENTRIES,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}]}})

    client = _client(handler)
    # 缓存满且含过期条目 → 淘汰过期后写入新条目，不清空
    _MODEL_CACHE.clear()
    _MODEL_CACHE[("stale", "e")] = (0.0, [])
    for i in range(MODEL_CACHE_MAX_ENTRIES - 1):
        _MODEL_CACHE[(f"fresh{i}", "e")] = (time.time(), [])
    assert len(_MODEL_CACHE) == MODEL_CACHE_MAX_ENTRIES
    await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert ("stale", "e") not in _MODEL_CACHE
    assert len(_MODEL_CACHE) == MODEL_CACHE_MAX_ENTRIES

    # 缓存满且全部新鲜 → 整体清空，只留本次写入
    _MODEL_CACHE.clear()
    for i in range(MODEL_CACHE_MAX_ENTRIES):
        _MODEL_CACHE[(f"fresh{i}", "e")] = (time.time(), [])
    await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert len(_MODEL_CACHE) == 1

async def test_fetch_models_parses_metadata():
    """模型元数据解析：倍率（credits 字符串）/ token 上限 / 支持性 / 推理档位。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "default", "name": "Default", "credits": "x2.00 credits",
             "maxInputTokens": 200000, "maxOutputTokens": 24000,
             "supportsImages": False, "supportsToolCall": True},
            {"id": "glm-5.3", "credits": "bad-format"},
            {"id": "kimi-k3", "supportsReasoning": True,
             "reasoning": {"effort": "medium", "summary": "auto"}},
            {"id": "no-effort", "supportsReasoning": False,
             "reasoning": {"summary": "auto"}},
            {"id": "bad-reasoning", "reasoning": "not-a-dict"},
            {"id": "bad-effort", "reasoning": {"effort": 7}},
        ]}})

    models = await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert models[0].credit_rate == 2.0
    assert models[0].max_input_tokens == 200000
    assert models[0].max_output_tokens == 24000
    assert models[0].supports_images is False
    assert models[0].supports_tool_call is True
    # 未提供推理元数据 → 留空
    assert models[0].supports_reasoning is None
    assert models[0].default_effort is None
    # 格式不符 → 留空，不影响条目
    assert models[1].credit_rate is None
    # reasoning.effort → 默认档位；supportsReasoning → 能力标志
    assert models[2].supports_reasoning is True
    assert models[2].default_effort == "medium"
    assert models[3].supports_reasoning is False
    assert models[3].default_effort is None
    # reasoning 非 dict / effort 非字符串 → 一律留空（不编造）
    assert models[4].default_effort is None
    assert models[5].default_effort is None

@pytest.mark.parametrize("body", [
    {"code": 1, "msg": "no"},
    {"code": 0, "data": {}},
    {"code": 0, "data": {"models": "no"}},
    {"code": 0, "data": {"models": []}},
    "not-json",
])
async def test_fetch_models_rejects_bad_responses(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))

async def test_fetch_models_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"unauthorized")

    with pytest.raises(UpstreamHTTPError):
        await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))

async def test_provider_list_models_falls_back_to_static_on_failure():
    """动态拉取失败 → 回退静态表，保证客户端始终有可用列表。"""
    provider = CodeBuddyProvider(client=_client(
        lambda _r: httpx.Response(500, content=b"down")))
    models = await provider.list_models({"bearer_token": "t"})
    assert [m.id for m in models] == list(DEFAULT_MODELS)

async def test_provider_list_models_uses_dynamic_when_available():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}, {"id": "glm-5.3"},
        ]}})

    provider = CodeBuddyProvider(client=_client(handler))
    models = await provider.list_models({"bearer_token": "t"})
    assert [m.id for m in models] == ["glm-5.2", "glm-5.3"]

async def test_fetch_models_rejects_non_object_config():
    """config 返回 JSON 数组 → 显式失败（client 173-174）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))

def test_error_type_for_invalid():
    """INVALID → invalid_request（executor 290-291）。"""
    from src.engine.executor import _error_type_for

    assert _error_type_for(ErrKind.INVALID) == "invalid_request"

def test_sanitize_channel_markers_neutralizes_prompt_roles_only():
    """system/assistant 正文替换为占位符；user/tool 与非正文位置不动。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [
        {"role": "system", "content": f"前缀 {marker} 后缀"},
        {"role": "assistant", "content": marker},
        {"role": "user", "content": marker},                      # user 不动
        {"role": "tool", "tool_call_id": "t", "content": marker},  # tool 不动
        {"role": "assistant", "tool_calls": [{"id": "c", "type": "function",
                                             "function": {"name": "f",
                                                          "arguments": f'"{marker}"'}}]},
    ]}
    hits = sanitize_channel_markers(body)
    assert hits == 2
    assert body["messages"][0]["content"] == "前缀 [external-client-identity] 后缀"
    assert body["messages"][1]["content"] == "[external-client-identity]"
    assert body["messages"][2]["content"] == marker
    assert body["messages"][3]["content"] == marker
    args = body["messages"][4]["tool_calls"][0]["function"]["arguments"]
    assert marker in args                          # tool_calls 参数不动

def test_sanitize_channel_markers_neutralizes_text_blocks():
    """content 为文本块列表时逐块中和（2026-09-21 实测块形态指纹同样 11128）。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [
        {"role": "system", "content": [
            {"type": "text", "text": f"前缀 {marker} 后缀"},
            {"type": "text", "text": "无指纹"},
            {"type": "image_url", "image_url": {"url": marker}},  # 非文本块不动
            {"type": "text", "text": 123},                        # 非 str 不动
            "junk",                                               # 非 dict 不动
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": marker},
            {"type": "text", "text": f"{marker} x{marker}"},
        ]},
        {"role": "user", "content": [{"type": "text", "text": marker}]},  # user 不动
    ]}
    assert sanitize_channel_markers(body) == 4
    blocks = body["messages"][0]["content"]
    assert blocks[0]["text"] == "前缀 [external-client-identity] 后缀"
    assert blocks[1]["text"] == "无指纹"
    assert blocks[2]["image_url"]["url"] == marker
    assert blocks[3]["text"] == 123
    assert blocks[4] == "junk"
    assistant_blocks = body["messages"][1]["content"]
    assert assistant_blocks[0]["text"] == "[external-client-identity]"
    assert assistant_blocks[1]["text"] == "[external-client-identity] x[external-client-identity]"
    assert body["messages"][2]["content"][0]["text"] == marker

def test_sanitize_channel_markers_counts_repeats_and_skips_noise():
    """同条消息多指纹多出处计数；非 list/非 dict/非 str content 安全跳过。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    a, b = CHANNEL_MARKERS[1], CHANNEL_MARKERS[2]
    body = {"messages": [
        "junk",
        {"role": "assistant", "content": None},
        {"role": "assistant"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": {"list": "content"}},
        {"role": "assistant", "content": f"{a} 中间 {b} 结尾 {a}"},
    ]}
    assert sanitize_channel_markers(body) == 3
    assert body["messages"][5]["content"] == (
        "[external-client-identity] 中间 [external-client-identity] 结尾 "
        "[external-client-identity]")

    assert sanitize_channel_markers({}) == 0
    assert sanitize_channel_markers({"messages": "nope"}) == 0
