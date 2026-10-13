"""凭证刷新与多账号切换，以及刷新后台任务（到期筛选、终端失败标记 relogin、模型冷却清理）。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.scheduler import ErrorOutcome, ModelCooldown
from src.main import build_app
from src.provider.base import ErrKind, Quota, UpstreamReloginRequired
from src.provider.codebuddy.checkin import CheckinResult
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.refresh import Account, CodeBuddyRefresh
from src.tasks.refresh import RefreshTask
from tests.helpers.constants import SECRET


def _refresh_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)

SWITCH_PATH = "/v2/plugin/login/enterprise"

@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

class ProbeProvider:
    id = "codebuddy"

    def __init__(self, quota=None, error=None, checkin_ok=True, refresh_new=None):
        self.quota = quota or Quota(remaining=8, total=10, probed_at=1)
        self.error = error
        self.checkin_ok = checkin_ok
        self.refresh_new = refresh_new
        self.checkin_calls = 0
        self.refresh_calls = 0
        self.seen_scopes: list[str] = []

    async def probe_quota(self, _data):
        if self.error is not None:
            raise self.error
        return self.quota

    async def checkin(self, _data):
        self.checkin_calls += 1
        return CheckinResult(ok=self.checkin_ok, credit=10 if self.checkin_ok else None)

    def checkin_scope(self, data):
        scope = f"acct:{data.get('account_uid', '')}"
        self.seen_scopes.append(scope)
        return scope

    def credential_from(self, data):
        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):
        self.refresh_calls += 1
        return {**data, "bearer_token": self.refresh_new or "NEW"}

def _disabled_outcome():
    from src.engine.scheduler import Candidate, Scheduler

    return Scheduler().note_error(Candidate(credential_id="c", provider="codebuddy"),
                                  ErrKind.DEAD, 0)

@pytest.fixture()
def admin_client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client

def _jwt(exp: int, *, iat: int | None = None,
         header: dict | None = None, payload_extra: dict | None = None) -> str:
    """构造测试用 JWT（不签名；本地只解码不验签）。"""
    body: dict = {"exp": exp}
    if iat is not None:
        body["iat"] = iat
    body.update(payload_extra or {})
    return f"{_b64(header or {'alg': 'RS256', 'typ': 'JWT'})}.{_b64(body)}.sig"

def _b64(obj: object) -> str:
    """base64url 编码任意可 JSON 序列化对象（含数组，用于负例）。"""
    import base64

    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

async def test_refresh_updates_token_and_reports_rotation():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": [
                {"accountId": "a2", "nickname": "second", "pluginEnabled": True}]}})
        return httpx.Response(200, json={"code": 0, "data": {
            "accessToken": "NEW", "refreshToken": "RT2", "expiresIn": 60}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT1", auth_source="oauth", user_id="u"))
    assert outcome.credential.bearer_token == "NEW"
    assert outcome.credential.refresh_token == "RT2"
    assert outcome.refresh_token_rotated is True
    assert outcome.accounts is not None and outcome.accounts[0].account_id == "a2"
    assert outcome.credential.auth_source == "oauth"      # 来源必须继承
    await client.aclose()

async def test_refresh_keeps_old_token_when_response_has_none():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": 5}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    original = CodeBuddyCredential(bearer_token="old", refresh_token="RT",
                                   auth_source="oauth")
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(original)
    assert original.bearer_token == "old" and original.refresh_token == "RT"
    await client.aclose()

async def test_refresh_requires_refresh_token():
    client = CodeBuddyRefresh("https://e", client=_refresh_client(
        lambda _r: httpx.Response(200, json={"code": 0})))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="x", auth_source="oauth"))
    await client.aclose()

async def test_refresh_marks_accounts_pending_on_failure():
    """账号同步失败 → accounts_pending（可恢复），但 token 已刷新。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.accounts_pending is True and outcome.accounts is None
    await client.aclose()

@pytest.mark.parametrize("account_response", [
    httpx.Response(200, content=b"<html>"),
    httpx.Response(200, json={"code": 1}),
    httpx.Response(200, json={"code": 0, "data": {"accounts": "no"}}),
    httpx.Response(200, json={"code": 0, "data": {}}),
])
async def test_accounts_unavailable_shapes(account_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return account_response
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "N"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.list_accounts(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

async def test_refresh_survives_accounts_transport_error():
    """账号阶段传输失败 → pending（可恢复），token 刷新本身成功。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.credential.bearer_token == "NEW"
    assert outcome.accounts_pending is True
    await client.aclose()

async def test_refresh_transport_error_on_token_stage_raises():
    """token 阶段传输失败必须显式失败，让调用方重试（不能静默当成功）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(
            bearer_token="old", refresh_token="RT", auth_source="oauth"))
    await client.aclose()

async def test_switch_account_clears_enterprise_context_for_personal():
    """切到个人账号必须清空企业上下文（AGENTS.md：禁止回退旧 enterprise_id）。"""
    seen: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            seen.append((request.url.path, dict(request.headers)))
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "SWITCHED", "refreshToken": "RT2"}})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "personal1", "nickname": "me", "type": "personal",
             "pluginEnabled": True},
            {"accountId": "ent1", "nickname": "corp", "type": "enterprise",
             "enterpriseId": "ent-9", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    base = CodeBuddyCredential(bearer_token="t", user_id="u", enterprise_id="old-ent",
                               department_full_name="技术部", auth_source="oauth",
                               refresh_token="RT")
    personal = await client.switch_account(base, "personal1")
    assert personal.enterprise_id == "" and personal.department_full_name == ""
    assert personal.account_uid == "personal1"
    assert personal.bearer_token == "SWITCHED"          # 切换会换 token
    assert seen[-1][0] == SWITCH_PATH                  # 个人账号走无后缀路径

    corporate = await client.switch_account(base, "ent1")
    assert corporate.enterprise_id == "ent-9" and corporate.department_full_name == "技术部"
    assert seen[-1][0] == f"{SWITCH_PATH}/ent-9"       # 企业账号带 enterprise_id
    assert "x-enterprise-id" in {k.lower() for k in seen[-1][1]}
    assert "x-tenant-id" in {k.lower() for k in seen[-1][1]}
    await client.aclose()

async def test_switch_account_rejects_disabled_target_account():
    """切换后目标账号不在启用列表 → account_missing。"""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "N"}})
        calls["n"] += 1
        accounts = [{"accountId": "a1", "type": "personal", "pluginEnabled": True}]
        if calls["n"] > 1:                              # 切换后的复查：账号被禁用
            accounts[0]["pluginEnabled"] = False
        return httpx.Response(200, json={"code": 0, "data": {"accounts": accounts}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "a1")
    await client.aclose()

async def test_switch_account_rejects_enterprise_without_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "e1", "type": "enterprise", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "e1")
    await client.aclose()

@pytest.mark.parametrize("account_id", ["", "ghost"])
async def test_switch_account_rejects_missing(account_id):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/switch"):
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1"}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(CodeBuddyCredential(bearer_token="t"), account_id)
    await client.aclose()

@pytest.mark.parametrize("switch_response", [
    httpx.Response(409, content=b"conflict"),
    httpx.Response(200, content=b"<html>"),
    httpx.Response(200, json={"code": 7}),
    httpx.Response(200, json={"code": 10081}),
])
async def test_switch_account_rejects_upstream_failure(switch_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return switch_response
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(CodeBuddyCredential(bearer_token="t",
                                                        refresh_token="RT"), "a1")
    await client.aclose()

async def test_refresh_lazy_client_and_close():
    client = CodeBuddyRefresh("https://e")
    assert client._http is client._http
    await client.aclose()

def test_to_account_skips_objects_without_id():
    from src.provider.codebuddy.refresh import _to_account

    assert _to_account("junk") is None
    assert _to_account({}) is None
    assert _to_account({"uid": "u", "pluginEnabled": True}) == Account(
        account_id="u", enabled=True)
    assert _to_account({"id": "i", "name": "n", "type": "personal"}).nickname == "n"

async def test_refresh_task_only_touches_due_credentials(repo):
    credentials, _db = repo
    now = 1_000_000
    soon = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": now + 100})
    later = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": now + 999_999})
    provider = ProbeProvider(refresh_new="NEW")
    task = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                       now=lambda: now)
    report = await task.run_once()
    assert report.succeeded == 1 and provider.refresh_calls == 1
    assert credentials.credential_data(soon)["bearer_token"] == "NEW"
    assert credentials.credential_data(later)["bearer_token"] == "old"

async def test_refresh_task_marks_relogin_required_on_terminal_failure(repo):
    """一次性续期凭据被作废 → 标记「需重新登录」并停止重试。

    旧行为是每轮任务刷一条一模一样的 warning：refresh_token 已废，重试永远
    成功不了。标记后本轮记 failed，下轮 `candidates()` 把它计入 skipped，
    管理台按 disabled_reason 提示需要人工重新登录。
    """
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1_000_100})

    class DeadProvider(ProbeProvider):
        async def refresh(self, _data):
            self.refresh_calls += 1
            raise UpstreamReloginRequired("刷新令牌已失效，需重新登录")

    provider = DeadProvider()
    task = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                       now=lambda: 1_000_000)
    report = await task.run_once()
    assert report.failed == 1 and report.succeeded == 0
    row = credentials.list_all()[0]
    assert row["disabled"] == 1
    assert row["disabled_reason"] == "刷新令牌已失效，需重新登录"
    # 凭证字段未被失败路径改写（旧票仍原样留着，供管理员复核）
    assert credentials.credential_data(credential_id)["bearer_token"] == "old"
    # 第二轮：已硬禁用 → 直接跳过，不再发刷新请求
    again = await task.run_once()
    assert again.skipped == 1 and provider.refresh_calls == 1

async def test_refresh_task_relogin_clears_model_cooldowns(repo):
    """标记「需重新登录」同时清掉模型级冷却，避免残留负缓存误导排查。"""
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1_000_100})
    credentials.save_error(credential_id, ErrorOutcome(
        model_cooldowns={"m": ModelCooldown(cooling_until=9_999_999_999, hits=1,
                                           reason="model")}))
    assert credentials.model_cooldowns_for(credential_id) == {"m": 9_999_999_999}

    class DeadProvider(ProbeProvider):
        async def refresh(self, _data):
            raise UpstreamReloginRequired("刷新令牌已失效，需重新登录")

    report = await RefreshTask(credentials, {"codebuddy": DeadProvider()},
                               skew_seconds=3600, now=lambda: 1_000_000).run_once()
    assert report.failed == 1
    assert credentials.candidates()[0].disabled is True
    assert credentials.model_cooldowns_for(credential_id) == {}

async def test_refresh_task_skips_manual_and_missing(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t",
                                                          "auth_source": "manual"})
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = RefreshTask(credentials, {"codebuddy": ProbeProvider()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 2

async def test_refresh_task_isolates_failures(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})

    class Boom(ProbeProvider):
        async def refresh(self, _data):
            raise RuntimeError("boom")

    task = RefreshTask(credentials, {"codebuddy": Boom()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.failed == 1

async def test_refresh_task_skips_credential_deleted_before_lock(repo):
    """等编辑锁期间凭证被删 → 持锁后重读为 None，记 skipped 而不是崩溃（M12）。"""
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})

    class Vanishing(ProbeProvider):
        async def refresh(self, _data):
            raise AssertionError("凭证已删，不应发起刷新")

    # 先占住编辑锁，让 run_once 在锁上等待；等它挂起后删掉凭证再放锁
    lock = credentials.edit_lock(credential_id)
    await lock.acquire()

    async def release_after_delete() -> None:
        await asyncio.sleep(0.01)
        credentials.delete(credential_id)
        lock.release()

    task = RefreshTask(credentials, {"codebuddy": Vanishing()}, skew_seconds=3600,
                       now=lambda: 1)
    _, report = await asyncio.gather(release_after_delete(), task.run_once())
    assert report.skipped == 1 and report.attempted == 1

def test_needs_refresh_without_builder_returns_false():
    from src.tasks.refresh import _needs_refresh

    assert _needs_refresh(object(), {}, 10, 0) is False

async def test_refresh_rejects_unauthorized_and_server_error():
    # 401/403 = 续期凭据被拒，终态：程序侧换不回新 token，只能重新登录
    for status in (401, 403):
        async def handler(_request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, content=b"x")

        client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
        with pytest.raises(UpstreamReloginRequired):
            await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                     auth_source="oauth"))
        await client.aclose()
    # 5xx 是可重试的失败：必须留在 UpstreamProtocolViolation，判成终态会让一次
    # 偶发故障永久废掉一条好凭证
    for status in (500, 503):
        async def handler(_request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, content=b"x")

        client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
        with pytest.raises(UpstreamProtocolViolation):
            await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                     auth_source="oauth"))
        await client.aclose()

async def test_refresh_rejects_non_json_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                auth_source="oauth"))
    await client.aclose()

async def test_refresh_rejects_business_error_code():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40001, "msg": "nope"})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                auth_source="oauth"))
    await client.aclose()

async def test_refresh_keeps_old_refresh_token_when_not_rotated():
    """上游没轮换 refresh token 时必须沿用旧值，不能清空。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": []}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.credential.refresh_token == "RT"
    assert outcome.refresh_token_rotated is False
    await client.aclose()

async def test_refresh_task_skips_disabled(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})
    credentials.save_error(credential_id, _disabled_outcome())
    task = RefreshTask(credentials, {"codebuddy": ProbeProvider()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.skipped == 1

async def test_refresh_survives_generic_exception_in_accounts_stage():
    """账号阶段抛非 httpx 异常也要记为 pending（refresh 125-130）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            raise RuntimeError("weird transport")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.accounts_pending is True
    await client.aclose()

async def test_switch_account_transport_error_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "a1")
    await client.aclose()

def test_switch_account_triggers_immediate_probe(admin_client):
    """账号切换后额度对应新账号，必须重探测而不是沿用旧值。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def switch_account(self, data, account_id):
            return {**data, "account_uid": account_id}

        async def probe_quota(self, _data):
            probed.append(1)
            return Quota(remaining=1, total=4, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{credential_id}/accounts/select",
                json={"account_id": "a1"})

    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == [1]

def test_refresh_task_refreshes_codebuddy_without_explicit_expiry(tmp_path):
    """回归：CB 凭证只有 JWT exp 时也要被预刷新（修复前 needs_refresh 恒 False）。

    这是 B3.3 顺带修掉的真实故障：token 到期后上游 401 → DEAD 硬禁用，
    而 revive 不会补刷新，凭证无法自愈。
    """
    import asyncio

    from src.tasks.refresh import RefreshTask

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    expiring = credentials.add(provider="codebuddy", now=now, credential_data={
        "bearer_token": _jwt(now + 3600), "refresh_token": "r", "auth_source": "oauth"})
    fresh = credentials.add(provider="codebuddy", now=now, credential_data={
        "bearer_token": _jwt(now + 40 * 86400), "refresh_token": "r",
        "auth_source": "oauth"})
    unknown = credentials.add(provider="codebuddy", now=now, credential_data={
        "bearer_token": "plain", "refresh_token": "r", "auth_source": "oauth"})

    refreshed_ids: list[str] = []

    class Provider:
        id = "codebuddy"

        def credential_from(self, data):
            return CodeBuddyCredential.from_dict(data)

        async def refresh(self, data):
            refreshed_ids.append(data["bearer_token"])
            return {"bearer_token": _jwt(now + 90 * 86400, iat=now), "refresh_token": "r",
                    "auth_source": "oauth"}

    task = RefreshTask(credentials, {"codebuddy": Provider()}, skew_seconds=24 * 3600,
                       now=lambda: now)
    report = asyncio.run(task.run_once())
    assert report.attempted == 1 and report.succeeded == 1
    assert len(refreshed_ids) == 1
    # 只有进入窗口的那条被刷新，到期时间与签发时间都推进
    row = db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (expiring,)).fetchone()
    assert tuple(row) == (now + 90 * 86400, now)
    untouched = db.connect().execute(
        "SELECT token_expires_at FROM credentials WHERE id = ?", (fresh,)).fetchone()[0]
    assert untouched == now + 40 * 86400
    # 到期时间未知的凭证不刷新（未知不等于紧急，避免每轮空打上游）
    assert db.connect().execute(
        "SELECT token_expires_at FROM credentials WHERE id = ?",
        (unknown,)).fetchone()[0] == 0
    db.close()
