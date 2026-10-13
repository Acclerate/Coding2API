"""CodeArtsProvider 委托与导入。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.codearts import CodeArtsProvider, credential_key, dpop
from src.provider.codearts import events as codearts_events
from src.provider.codearts.client import CodeArtsClient, UpstreamHTTPError
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

def test_provider_id_classify_credential_from_and_key():
    provider = CodeArtsProvider()
    assert provider.id == "codearts"
    assert provider.classify(401, b"") is ErrKind.DEAD
    credential = provider.credential_from({"access_key_id": "AK", "uid": "u"})
    assert credential.access_key_id == "AK"
    from src.tasks.pacer import stable_key
    assert credential_key(credential) == stable_key("codearts", "u")
    assert credential_key(_cred(uid="")) == stable_key("codearts", "AK")

def test_provider_import_credential_validates():
    provider = CodeArtsProvider()
    valid = provider.import_credential({"access_key_id": "AK",
                                        "secret_access_key": "SK"})
    assert valid["access_key_id"] == "AK"
    # dpop_private_jwk 可缺（手工凭证无从刷新）
    assert valid["dpop_private_jwk"] == {}
    with_jwk = provider.import_credential({
        "access_key_id": "AK", "secret_access_key": "SK",
        "dpop_private_jwk": _jwk()})
    assert with_jwk["dpop_private_jwk"]["kty"] == "EC"

def test_provider_import_credential_rejects_bad_input():
    provider = CodeArtsProvider()
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="not an object"):
        provider.import_credential("nope")               # type: ignore[arg-type]
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="missing"):
        provider.import_credential({"access_key_id": "AK"})
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="dpop_private_jwk"):
        provider.import_credential({"access_key_id": "AK", "secret_access_key": "SK",
                                    "dpop_private_jwk": {"kty": "RSA"}})

async def test_provider_list_models_refresh_and_probe_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(200, json={"builtinModels": [{"id": "m1"}]})
        if request.url.path == codearts_events.EP_BENEFIT_CONFIG:
            return httpx.Response(200, json={"result": {"models": []}})
        if request.url.host == "sts.test":
            return httpx.Response(200, json=_token_response())
        return httpx.Response(200, json={"remaining": 9})

    provider = CodeArtsProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m1"]
    refreshed = await provider.refresh({"access_key_id": "AK", "secret_access_key": "SK",
                                        "refresh_token": "rt", "dpop_private_jwk": _jwk()})
    assert refreshed["access_key_id"] == "AK2"
    assert (await provider.probe_quota({})).remaining == pytest.approx(9 / 10000)
    await provider.aclose()

async def test_provider_probe_quota_discovers_benefit_catalog_per_account():
    """额度探测逐凭证跑，顺带补该账号自己的福利目录。

    模型列表刷新只用 `candidates[0]` 一个凭证，其余账号的 uid 永不上册，
    `is_benefit_model` 退回冷启动种子；种子装不下「按期授予」的福利模型
    （如 `deepseek-v4.1-flash`），那些账号的请求就漏带 `maas_type: benefit`
    头——请求照样 200，但每日 token 池不扣、credit 不记。探测遍历全部凭证，
    正是补齐点。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(200, json={"builtinModels": [{"id": "m1"}]})
        if request.url.path == codearts_events.EP_BENEFIT_CONFIG:
            return httpx.Response(200, json={"result": {"models": [
                {"id": "deepseek-v4.1-flash"}]}})
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(200, json={"remaining": 9})

    provider = CodeArtsProvider(client=_client(handler))
    data = {"uid": "u1", "access_key_id": "AK", "secret_access_key": "SK"}
    assert (await provider.probe_quota(data)).remaining == pytest.approx(9 / 10000)
    # 本账号目录已入册：种子外的新福利模型也判 True（否则漏带 benefit 头）。
    assert provider.client.is_benefit_model(_cred(), "deepseek-v4.1-flash") is True
    await provider.aclose()

async def test_provider_probe_quota_keeps_balance_when_catalog_discovery_fails(caplog):
    """目录发现失败必须降级：余额照常返回、只记 warning，不拖垮探测。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_TOKEN_BALANCE:
            return httpx.Response(200, json={"remaining": 9})
        return httpx.Response(500, content=b"down")

    provider = CodeArtsProvider(client=_client(handler))
    with caplog.at_level("WARNING"):
        quota = await provider.probe_quota(
            {"uid": "u1", "access_key_id": "AK", "secret_access_key": "SK"})
    assert quota.remaining == pytest.approx(9 / 10000)
    assert any("福利目录刷新失败" in record.getMessage() for record in caplog.records)
    await provider.aclose()

async def test_provider_stream_chat_pacer_pairing_and_release_on_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse({"text": "hi"}, {"text": "[DONE]"}))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = CodeArtsProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat(
        {"access_key_id": "AK", "secret_access_key": "SK"},
        {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1 and pacer.released == 1
    await provider.aclose()

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    pacer2 = Pacer()
    error_provider = CodeArtsProvider(client=_client(boom), pacer=pacer2)
    with pytest.raises(UpstreamHTTPError):
        [e async for e in error_provider.stream_chat(
            {"access_key_id": "AK", "secret_access_key": "SK"}, {}, "m")]
    assert pacer2.released == 1                        # 出错也要归还名额
    await error_provider.aclose()

    plain = CodeArtsProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat(
        {"access_key_id": "AK", "secret_access_key": "SK"}, {}, "m")]
    await plain.aclose()
