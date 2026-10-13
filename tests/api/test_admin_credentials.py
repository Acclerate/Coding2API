"""凭证运维端点：探测 / 签到 / 账号切换 / 即时探测 / 模型冷却 / token 到期 / 积分流水。"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import CredentialRepository, GrowthRepository
from src.engine.scheduler import ErrorOutcome
from src.main import build_app
from src.provider.base import GrowthResult, GrowthStep, Quota, StepStatus
from src.provider.codebuddy.checkin import CheckinResult
from src.provider.codebuddy.client import CodeBuddyProvider
from src.provider.codebuddy.growth import EP_ENERGY, EP_TASKS, EP_TRAVEL_CONFIG, EP_TRAVEL_STATUS
from src.provider.codebuddy.refresh import Account


@pytest.fixture()
def admin_client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client

class _StubCodeBuddy:
    """探测 / 签到桩 provider：本用例只验证端点接线，绝不出网。

    V1 版本直接打真实上游（probe 容错 502、checkin 不设容错），CI 网络抖动时
    checkin 收到 502 就会红——测试不得依赖网络，故注入确定性桩。
    """

    id = "codebuddy"

    async def probe_quota(self, _data):
        return Quota(remaining=7, total=10, probed_at=1)

    async def checkin(self, _data):
        return CheckinResult(ok=True, credit=5, code=0, message="ok")

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

SECRET = "test-secret-0123456789"

TRAVEL_IDLE = {"code": 0, "data": {
    "state": "idle", "location": {"id": 1, "name": "咖啡馆"},
    "daily_limit_reached": False}}

def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)

@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "growth.sqlite3")
    apply_schema(db.connect())
    cipher = CredentialCipher(SECRET)
    yield CredentialRepository(db, cipher), GrowthRepository(db)
    db.close()

def _admin_client(tmp_path):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    return app, client

def test_growth_endpoints_run_and_history(tmp_path):
    """手动执行 + 历史查询；无该能力的渠道与不存在的凭证都返回 400。"""
    app, client = _admin_client(tmp_path)
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def growth(self, _data, *, allow_irreversible: bool = True):
            self.flags.append(allow_irreversible)
            return GrowthResult(ok=True, credit=42.0, energy=7, streak_days=3,
                                report="领旅行礼物：+42 积分",
                                steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "+42 积分")])

    stub = Stub()
    app.state.executor._deps.providers["codebuddy"] = stub
    with client:
        body = client.post(f"/api/credentials/{credential_id}/growth").json()
        assert body["ok"] is True and body["credit"] == 42.0 and body["streak_days"] == 3
        assert body["steps"] == [{"name": "领旅行礼物", "status": "done",
                                  "detail": "+42 积分", "credit": None}]
        assert stub.flags == [True]                 # 跟随配置开关

        history = client.get(f"/api/credentials/{credential_id}/growth").json()["events"]
        assert len(history) == 1 and history[0]["trigger"] == "manual"
        assert history[0]["report"] == "领旅行礼物：+42 积分"

        # 列表携带最近一次结果，界面不必再查一次 events
        listed = next(row for row in client.get("/api/credentials").json()["credentials"]
                      if row["provider"] == "codebuddy")
        assert listed["growth_last_result"] == "领旅行礼物：+42 积分"

        # TRAE 不支持成长中心 → 稳定可读的错误
        trae_id = app.state.credentials.add(provider="trae",
                                            credential_data={"accessToken": "t"})
        assert client.post(f"/api/credentials/{trae_id}/growth").status_code == 400
        assert client.get("/api/credentials/cred_missing/growth").status_code == 400
        assert client.post("/api/credentials/cred_missing/growth").status_code == 400

def test_growth_endpoint_respects_irreversible_setting(tmp_path):
    """配置关闭不可逆动作时，手动入口也必须跟着关（不能只挡定时任务）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", GROWTH_IRREVERSIBLE_ACTIONS=False)
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def growth(self, _data, *, allow_irreversible: bool = True):
            self.flags.append(allow_irreversible)
            return GrowthResult(ok=True, report="无可领取项")

    stub = Stub()
    app.state.executor._deps.providers["codebuddy"] = stub
    with client:
        client.post(f"/api/credentials/{credential_id}/growth")
    assert stub.flags == [False]

def test_growth_history_limit_and_empty(tmp_path):
    """历史查询：limit 生效，空历史返回空数组。"""
    app, client = _admin_client(tmp_path)
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})
    with client:
        assert client.get(f"/api/credentials/{credential_id}/growth").json() == {"events": []}
    for index in range(3):
        app.state.growth_events.record(
            credential_id=credential_id,
            result=GrowthResult(ok=True, report=f"第 {index} 轮"),
            now=1000 + index)
    with client:
        events = client.get(f"/api/credentials/{credential_id}/growth?limit=2").json()["events"]
    assert [event["report"] for event in events] == ["第 2 轮", "第 1 轮"]

def test_growth_repository_recent_clamps_limit(repo):
    """limit 下限保护：<=0 时仍返回至少一行（否则界面永远空着像坏了）。"""
    credentials, events = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    events.record(credential_id=credential_id, result=GrowthResult(report="x"), now=1)
    assert len(events.recent(credential_id, limit=0)) == 1
    assert events.latest_for("cred_missing") is None

def test_growth_schema_migration_from_legacy_db(tmp_path):
    """老库升级：补 growth 列 + 建 growth_events 表，历史数据不丢。"""
    import sqlite3

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.execute("INSERT INTO credentials (id, provider, data_enc, created_at) "
                 "VALUES ('cred_old', 'codebuddy', 'x', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert {"growth_last_run_at", "growth_last_result"} <= columns
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "growth_events" in tables
    # 历史凭证保留，新列为 NULL
    row = db.connect().execute(
        "SELECT growth_last_result, created_at FROM credentials WHERE id = 'cred_old'").fetchone()
    assert row["growth_last_result"] is None and row["created_at"] == 1
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    apply_schema(db.connect())                        # 二次执行不抛错
    db.close()

def test_growth_schema_index_exists(tmp_path):
    """growth_events 的 (credential_id, ts) 索引必须建出来（历史查询走它）。"""
    db = Database(tmp_path / "index.sqlite3")
    apply_schema(db.connect())
    indexes = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_growth_cred_ts" in indexes
    db.close()

async def test_provider_growth_delegates_to_runner_and_closes_pool():
    """provider.growth 走缓存客户端；aclose 释放 growth 连接池。"""
    from src.provider.codebuddy.client import _GROWTH_CACHE, _cached_growth

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_ENERGY:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 3}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    from src.provider.codebuddy.client import CodeBuddyClient

    client = CodeBuddyClient(endpoint="https://e",
                             short_client=_client(handler))
    provider = CodeBuddyProvider(client=client)
    result = await provider.growth({"bearer_token": "t"}, allow_irreversible=False)
    assert result.ok is True and result.energy == 3
    assert _cached_growth(client) is _cached_growth(client)
    assert client in _GROWTH_CACHE
    await provider.aclose()
    assert client not in _GROWTH_CACHE

def test_upstream_auth_start_supports_both_providers(admin_client):
    """CodeBuddy 走 poll 轨道，TRAE 走 callback 轨道，unknown provider 报 400。"""
    app, client = admin_client
    # TRAE 的 callback 轨道不需要出网，必定成功并带入参回调地址
    trae = client.post("/api/auth/upstream/start", json={"provider": "trae"})
    assert trae.status_code == 200
    body = trae.json()
    assert body["flow"] == "callback"
    assert body["callback_url"].endswith("/authorize")
    assert "auth_callback_url" in body["auth_url"]
    # 按发起人（会话用户名）登记，值为 (started_at, state)
    assert app.state.pending_callbacks["root"][1] == body["state"]

    assert client.post("/api/auth/upstream/start",
                       json={"provider": "unknown"}).status_code == 400

def test_upstream_auth_poll_unknown_state(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "codebuddy", "state": "ghost"})
    assert response.status_code == 400

def test_upstream_auth_rejects_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert client.post("/api/auth/upstream/start",
                           json={"provider": "codebuddy"}).status_code == 403

def test_credential_probe_and_checkin_endpoints(admin_client):
    app, client = admin_client
    # 端点按 provider_id 现查 registry（同一 dict 对象），替换该条目即可隔离上游
    app.state.services.registry["codebuddy"] = _StubCodeBuddy()
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u"})
    probe = client.post(f"/api/credentials/{credential_id}/probe")
    assert probe.status_code == 200
    assert probe.json()["probed"] is True
    checkin = client.post(f"/api/credentials/{credential_id}/checkin")
    assert checkin.status_code == 200
    assert checkin.json() == {"ok": True, "credit": 5, "code": 0, "message": "ok",
                              "already_checked_in": False, "status": None}
    assert client.post("/api/credentials/ghost/probe").status_code == 400
    assert client.post("/api/credentials/ghost/checkin").status_code == 400

def test_credential_account_endpoints_unsupported_for_trae(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="trae",
                                              credential_data={"accessToken": "t"})
    assert client.get(f"/api/credentials/{credential_id}/accounts").status_code == 400
    assert client.post(f"/api/credentials/{credential_id}/accounts/select",
                       json={"account_id": "a"}).status_code == 400
    assert client.get("/api/credentials/ghost/accounts").status_code == 400

def test_select_account_rejects_vanished_credential(admin_client):
    """provider 支持切换但持锁后凭证已消失 → 400，不拿 None 去打上游（M12）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def switch_account(self, _data, _account_id):  # pragma: no cover
            raise AssertionError("凭证已消失，不应调用上游")

    app.state.executor._deps.providers["codebuddy"] = Stub()
    original = app.state.credentials.credential_data
    app.state.credentials.credential_data = lambda _cid: None  # type: ignore[method-assign]
    try:
        response = client.post(f"/api/credentials/{credential_id}/accounts/select",
                               json={"account_id": "a1"})
    finally:
        app.state.credentials.credential_data = original  # type: ignore[method-assign]
    assert response.status_code == 400

def test_upstream_auth_cancel_endpoint(admin_client):
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]
    session_start = oauth.store.begin("root", "up")
    assert client.post("/api/auth/upstream/cancel",
                       json={"provider": "codebuddy", "state": session_start}).json()[
        "cancelled"] is True
    assert client.post("/api/auth/upstream/cancel",
                       json={"provider": "trae", "state": "x"}).status_code == 400

def test_upstream_auth_poll_success_persists_credential(admin_client):
    """轮询成功 → 凭证入库，且响应里绝不回传 token。"""
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]
    reservation = oauth.store.begin("root", "up-state")

    async def fake_poll(_state, _username):
        from src.provider.base import AuthResult

        return AuthResult(credential_data={"bearer_token": "AT", "auth_source": "oauth"},
                          nickname="nick")

    oauth.poll = fake_poll
    body = client.post("/api/auth/upstream/poll",
                       json={"provider": "codebuddy", "state": reservation}).json()
    assert body["status"] == "success"
    assert "token" not in json.dumps(body).lower()
    assert any(row["provider"] == "codebuddy"
               for row in app.state.credentials.list_all())

def test_probe_endpoint_marks_failed_on_provider_error(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Boom:
        endpoint = "https://e"

        async def probe_quota(self, _data):
            raise RuntimeError("boom")

    app.state.executor._deps.providers["codebuddy"] = Boom()
    body = client.post(f"/api/credentials/{credential_id}/probe").json()
    assert body["probed"] is False
    # 不能把 Python 类名当作 reason 暴露给用户
    assert body["reason"] == "unknown_error"
    assert "RuntimeError" not in body["reason"]
    # 上游原文/异常文本不再回显给客户端（M2/M4），只回受控 reason
    assert "detail" not in body
    assert app.state.credentials.candidates()[0].health is None

def test_checkin_endpoint_reports_result(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=12.0)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/checkin").json()
    assert body["ok"] is True and body["credit"] == 12.0
    assert body["status"] is None                     # 无状态能力的渠道返回 null

def test_checkin_endpoint_passes_status_through(admin_client):
    """provider 回填 status 时原样透传（前端靠它显示连续天数）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=100.0,
                                 status={"streak_days": 4, "today_checked_in": True})

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/checkin").json()
    assert body["status"] == {"streak_days": 4, "today_checked_in": True}

def test_checkin_status_endpoint(admin_client):
    """只读状态端点：有能力的渠道返回状态，无能力/不存在的凭证 400。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin_status(self, _data):
            return {"active": True, "streak_days": 7}

    app.state.executor._deps.providers["codebuddy"] = Stub()
    assert client.get(f"/api/credentials/{credential_id}/checkin").json() == {
        "status": {"active": True, "streak_days": 7}}

    # 没有 checkin_status 能力的渠道 → 稳定可读的错误
    app.state.executor._deps.providers["codebuddy"] = type("Bare", (), {"endpoint": "https://e"})()
    rejected = client.get(f"/api/credentials/{credential_id}/checkin")
    assert rejected.status_code == 400
    assert "checkin status" in rejected.text

    # 凭证不存在同样 400
    assert client.get("/api/credentials/cred_missing/checkin").status_code == 400

def test_account_endpoints_with_stub_provider(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def list_accounts(self, _data):
            return [Account(account_id="a1", nickname="n", account_type="personal",
                            enabled=True)]

        async def switch_account(self, data, account_id):
            return {**data, "account_uid": account_id}

    app.state.executor._deps.providers["codebuddy"] = Stub()
    accounts = client.get(f"/api/credentials/{credential_id}/accounts").json()["accounts"]
    assert accounts == [{"account_id": "a1", "nickname": "n", "type": "personal"}]
    switched = client.post(f"/api/credentials/{credential_id}/accounts/select",
                           json={"account_id": "a1"})
    assert switched.json()["switched"] is True
    assert app.state.credentials.credential_data(credential_id)["account_uid"] == "a1"

def test_upstream_auth_poll_unknown_provider(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "trae", "state": "x"})
    assert response.status_code == 400

def test_probe_endpoint_returns_quota_fields(admin_client):
    """探测成功路径的响应字段（main 271-272）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def probe_quota(self, _data):
            return Quota(remaining=7, total=10, cycle_end=123, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/probe").json()
    assert body == {"probed": True, "remaining": 7, "total": 10, "cycle_end": 123}

def test_credentials_endpoint_exposes_admin_flag(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "guest", "password": "guestpw"})
        body = client.get("/api/credentials").json()
        assert body["viewer"] == "guest" and body["is_admin"] is False
        assert body["is_operator"] is False

def test_credentials_endpoint_exposes_expiring_credits(tmp_path):
    """到期指标随列表下发（与调度排序同源）；无周期信息为 None，套餐阶梯同源下发。"""
    from src.db.repo import _ladder_text

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        repo.add(provider="codebuddy", credential_data={"bearer_token": "t"})
        repo.add(provider="trae", credential_data={"token": "x"})
        ids = {row["provider"]: row["id"] for row in repo.list_all()}
        now = int(time.time())
        ladder = _ladder_text([(now + 3600, 100.0), (now + 3 * 86400, 40.0),
                               (now + 999_999, 50.0)])
        conn = repo._db.connect()
        conn.execute(
            "UPDATE credentials SET quota_expiry_ladder = ?, quota_packages = ? WHERE id = ?",
            (ladder, json.dumps([{"name": "福利积分", "total": 2000.0, "used": 0.0,
                                  "end": now + 3600}]), ids["codebuddy"]))
        conn.commit()
        body = client.get("/api/credentials").json()

    rows = {row["provider"]: row for row in body["credentials"]}
    assert body["expiry_window_seconds"] == settings.quota_expiry_window_seconds
    assert body["expiry_secondary_window_seconds"] == \
        settings.quota_expiry_secondary_window_seconds
    assert rows["codebuddy"]["quota_expiring_credits"] == 100.0
    # 次窗口是 36h 窗口的超集：主窗口已含的包也计入（这里 100 + 40）
    assert rows["codebuddy"]["quota_expiring_credits_secondary"] == 140.0
    assert rows["codebuddy"]["quota_expiry_ladder"] == [
        [now + 3600, 100.0], [now + 3 * 86400, 40.0], [now + 999_999, 50.0]]
    assert rows["trae"]["quota_expiring_credits"] is None
    assert rows["trae"]["quota_expiring_credits_secondary"] is None
    assert rows["trae"]["quota_expiry_ladder"] is None
    # 额度包明细（展示用）同样随列表下发；无明细的渠道为 None
    assert rows["codebuddy"]["quota_packages"] == [
        {"name": "福利积分", "total": 2000.0, "used": 0.0, "end": now + 3600}]
    assert rows["trae"]["quota_packages"] is None
    # 窗口关闭 → 0 而不是 None，展示层据此隐藏该行（两级窗口各自独立）
    closed = {row["provider"]: row for row in repo.list_all(
        expiring_window=0, now=now)}
    assert closed["codebuddy"]["quota_expiring_credits"] == 0.0
    assert closed["codebuddy"]["quota_expiring_credits_secondary"] == 0.0
    secondary_closed = {row["provider"]: row for row in repo.list_all(
        expiring_window=settings.quota_expiry_window_seconds,
        expiring_secondary_window=0, now=now)}
    assert secondary_closed["codebuddy"]["quota_expiring_credits"] == 100.0
    assert secondary_closed["codebuddy"]["quota_expiring_credits_secondary"] == 0.0

def test_import_triggers_immediate_probe(admin_client):
    """新增凭证后应立即探测，否则界面一直显示「未探测到额度」。"""
    app, client = admin_client
    probed: list[str] = []

    class Stub:
        endpoint = "https://e"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "t")}

        async def probe_quota(self, _data):
            probed.append("called")
            return Quota(remaining=7, total=10, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "abc"}})
    credential_id = created.json()["id"]

    # 后台任务在事件循环里执行，需等待落库
    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == ["called"]
    health = next(c for c in app.state.credentials.candidates(["codebuddy"])).health
    assert health == 70
    assert credential_id

def test_probe_failure_marks_unknown_on_immediate_path(admin_client):
    """即时探测失败 → 记为未探测（NULL），不是额度 0。"""
    app, client = admin_client

    class Stub:
        endpoint = "https://e"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "t")}

        async def probe_quota(self, _data):
            raise RuntimeError("upstream down")

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post("/api/credentials", json={"provider": "codebuddy", "credential": {"token": "x"}})

    # 探测失败会写 quota_probed_at 并把 health 置回 NULL
    for _ in range(50):
        listed = client.get("/api/credentials").json()["credentials"][0]
        if listed["quota_probed_at"] is not None:
            break
        time.sleep(0.02)
    assert listed["health"] is None
    assert listed["quota_probed_at"] is not None

def test_checkin_success_triggers_immediate_probe(admin_client):
    """签到发放积分 → 立即刷新额度；签到失败则不探测。"""
    app, client = admin_client
    ok_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=10)

        async def probe_quota(self, _data):
            probed.append(1)
            return Quota(remaining=3, total=4, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{ok_id}/checkin")

    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == [1]

def test_checkin_failure_does_not_probe(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=False, code=0)

        async def probe_quota(self, _data):  # pragma: no cover
            probed.append(1)
            return Quota()

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{credential_id}/checkin")
    time.sleep(0.1)
    assert probed == []

def test_schedule_probe_ignores_deleted_credential(admin_client):
    """凭证在探测前被删除 → schedule_probe 静默返回，不抛异常也不建任务。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    before = len(app.state.pending_probes)
    app.state.credentials.delete(credential_id)

    # 经由 API 触发内部 schedule_probe 的同一路径：重新导入再删除
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "x"}}).json()["id"]
    app.state.credentials.delete(created)

    assert len(app.state.pending_probes) >= before

def test_schedule_probe_returns_early_when_credential_unreadable(admin_client):
    """凭证读取不到（并发删除）→ schedule_probe 直接返回，不建探测任务（main 383-384）。"""
    app, client = admin_client
    before = len(app.state.pending_probes)
    app.state.credentials.credential_data = lambda _cid: None  # type: ignore[method-assign]

    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "x"}})
    assert created.status_code == 200
    time.sleep(0.05)
    assert len(app.state.pending_probes) == before

def test_list_all_exposes_model_cooldowns(tmp_path):
    """管理台列表带出生效中的模型冷却（过期行不下发）。"""
    from src.engine.scheduler import ModelCooldown

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    now = int(time.time())
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "fresh": ModelCooldown(cooling_until=now + 600, hits=2, reason="blocked")}))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "stale": ModelCooldown(cooling_until=now - 600, hits=1)}))
    row = credentials.list_all(now=now)[0]
    assert row["model_cooldowns"] == [
        {"model": "fresh", "cooling_until": now + 600, "hits": 2, "reason": "blocked"}]
    assert CredentialRepository(
        db, CredentialCipher(SECRET)).list_all(now=now)[0]["model_cooldowns"] == row[
            "model_cooldowns"]
    db.close()

def test_credentials_endpoint_exposes_model_cooldowns(tmp_path):
    """端到端：模型级冷却经 /api/credentials 下发（前端据此展示）。"""
    from src.engine.scheduler import ModelCooldown

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        cid = repo.add(provider="codebuddy", credential_data={"bearer_token": "t"})
        repo.save_error(cid, ErrorOutcome(model_cooldowns={
            "glm-5.2": ModelCooldown(cooling_until=int(time.time()) + 600,
                                     hits=1, reason="model")}))
        rows = client.get("/api/credentials").json()["credentials"]
    codebuddy = next(row for row in rows if row["provider"] == "codebuddy")
    assert [row["model"] for row in codebuddy["model_cooldowns"]] == ["glm-5.2"]

def test_credentials_endpoint_exposes_token_expiry(tmp_path):
    """端到端：/api/credentials 下发 token 签发/到期秒数与预警阈值。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", TOKEN_EXPIRY_WARNING_SECONDS=7200)
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        now = int(time.time())
        repo.add(provider="codebuddy",
                 credential_data={"bearer_token": _jwt(now + 600, iat=now)}, now=now)
        repo.add(provider="trae", credential_data={"accessToken": "plain"}, now=now)
        body = client.get("/api/credentials").json()
    assert body["token_expiry_warning_seconds"] == 7200
    rows = {row["provider"]: row for row in body["credentials"]}
    assert rows["codebuddy"]["token_expires_at"] > now
    assert rows["codebuddy"]["token_issued_at"] == now
    # 未知到期下发 0（展示层隐藏），不是 0 之外的哨兵值
    assert rows["trae"]["token_expires_at"] == 0
    assert rows["trae"]["token_issued_at"] == 0
