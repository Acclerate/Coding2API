"""聊天出口端到端：错误分类与冷却、候选收窄、超时/事件上限、指纹与角色归一、API Key 鉴权。"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import ErrorOutcome, ModelCooldown, Scheduler
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Model, Usage
from src.provider.codebuddy.client import (
    CodeBuddyClient,
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
)
from src.provider.qoder.credential import QoderCredential
from src.provider.trae.events import UpstreamProtocolViolation
from src.tasks.pacer import stable_key
from tests.helpers.constants import SECRET
from tests.helpers.paths import provider_fixture


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

@pytest.fixture()
def dual_repo(tmp_path):
    db = Database(tmp_path / "dual.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

class DualProvider:
    """同一脚本双 provider：验证调度层对两个上游行为一致。"""

    def __init__(self, provider_id: str, script) -> None:
        self.id = provider_id
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return []

GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]

def _dual_executor(credentials, trae_script, cb_script, **kw):
    trae = DualProvider("trae", trae_script)
    codebuddy = DualProvider("codebuddy", cb_script)
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": codebuddy}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2"))
    return executor, trae, codebuddy

def _request(model="glm-5.2"):
    from src.compat.openai.request import parse_chat_request

    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": model})

class _RejectProvider:
    """可脚本化的 provider：按调用次序抛指定异常或返回事件。"""

    def __init__(self, provider_id: str, outcomes: list) -> None:
        self.id = provider_id
        self.outcomes = outcomes
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        item = self.outcomes[index]
        if isinstance(item, Exception):
            raise item
        for event in item:
            yield event

    def list_models(self, _credential_data):
        return []

def _http_400() -> Exception:
    from src.provider.codebuddy.client import UpstreamHTTPError

    return UpstreamHTTPError(400, b'{"code":1002,"msg":"model not found"}')

_CB_11128_BODY = (
    b'{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}')

def _model_cooldown_rows(db):
    return [tuple(row) for row in db.connect().execute(
        "SELECT credential_id, model, hits, reason FROM credential_model_cooldowns"
    ).fetchall()]

class FakeProvider:
    id = "trae"

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, credential_data, payload, model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _credential_data):
        return [Model(id="glm-5.2")]

    def import_credential(self, raw):
        if not raw.get("accessToken"):
            raise UpstreamProtocolViolation("credential missing accessToken")
        return dict(raw)

    def classify(self, status, body=b""):
        return ErrKind.OTHER

GOOD_trae = [Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
        Event(kind=EventKind.FINISH, finish_reason="stop")]

@pytest.fixture()
def client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": FakeProvider([GOOD_trae])})
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client

def _login(client, username="root"):
    from src.auth.session import create_session_token

    token = create_session_token(username, SECRET)
    client.cookies.set("coding2api_session", token)

def cred(**overrides) -> QoderCredential:
    base = {"access_token": "dt-token-1", "uid": "u-1", "realm": "cn",
            "refresh_token": "drt-1", "auth_source": "oauth",
            "nickname": "Tester", "expires_at": int(time.time()) + 86400}
    base.update(overrides)
    return QoderCredential(**base)

def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}

def test_api_requires_key(client):
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user"}]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"

def test_api_rejects_bad_key(client):
    response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"},
                           json={"messages": [{"role": "user"}]})
    assert response.status_code == 401

def test_full_flow_apikey_chat_and_models(client, tmp_path):
    app = client.app
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = app.state.api_keys.create("root", "test")
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    models = client.get("/v1/models", headers=headers)
    assert models.status_code == 200
    assert models.json()["data"][0]["providers"] == ["trae"]
    chat = client.post("/v1/chat/completions", headers=headers,
                       json={"messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "hi"

def test_api_validation_error(client):
    app = client.app
    key = app.state.api_keys.create("root")["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    response = client.post("/v1/chat/completions", headers=headers, json={"messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_admin_endpoints_require_session(client):
    assert client.get("/api/credentials").status_code == 401

def test_admin_credential_lifecycle(client):
    _login(client)
    created = client.post("/api/credentials", json={
        "provider": "trae", "credential": {"accessToken": "a", "uid": "u"}, "nickname": "n"})
    assert created.status_code == 200
    credential_id = created.json()["id"]
    assert client.get("/api/credentials").json()["credentials"][0]["id"] == credential_id
    pinned = client.post("/api/credentials/pin", json={"credential_id": credential_id})
    assert pinned.status_code == 200
    assert client.post(f"/api/credentials/{credential_id}/toggle",
                       json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/credentials/{credential_id}").status_code == 200
    assert client.delete(f"/api/credentials/{credential_id}").status_code == 400

def test_admin_import_rejects_unknown_provider(client):
    _login(client)
    response = client.post("/api/credentials", json={"provider": "nope", "credential": {}})
    assert response.status_code == 400

def test_admin_import_rejects_bad_credential(client):
    _login(client)
    response = client.post("/api/credentials", json={"provider": "trae", "credential": {}})
    assert response.status_code == 400

def test_api_key_crud(client):
    _login(client)
    created = client.post("/api/api-keys", json={"name": "k"})
    assert created.status_code == 200 and created.json()["api_key"].startswith("sk-")
    key_id = created.json()["id"]
    assert client.get("/api/api-keys").json()["api_keys"][0]["id"] == key_id
    assert client.delete(f"/api/api-keys/{key_id}").status_code == 200
    assert client.delete(f"/api/api-keys/{key_id}").status_code == 400

def test_non_admin_cannot_write(client):
    _login(client, username="guest")
    assert client.post("/api/credentials", json={"provider": "trae",
                                                 "credential": {}}).status_code == 403

def test_authorize_rejects_callback_without_pending_login(client):
    """没有进行中的登录时，任意回调不得被塞进凭证池。"""
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_authorize_expires_stale_pending_login(client):
    """待完成登录有 TTL：/authorize 无鉴权，长期挂着的 pending 会被同网络
    任何人用自己的 refreshToken 完成兑换（凭证塞进池子）。过期即作废。"""
    import time as _time

    client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
    client.app.state.pending_callbacks["root"] = (_time.monotonic() - 601, "machine:device")
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert "expired" in response.json()["error"]["message"]
    assert "root" not in client.app.state.pending_callbacks

def test_authorize_requires_initiating_session(client):
    """回调必须由发起登录的本人完成（M7）：会话缺失 → 拒绝。

    防止同网段的人用自己的 refreshToken 顶掉别人进行中的登录、把凭证塞进池子。
    """
    import time as _time

    client.app.state.pending_callbacks["root"] = (_time.monotonic(), "machine:device")
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    # 未被消费：真正的发起人仍可完成
    assert "root" in client.app.state.pending_callbacks

async def test_http_400_does_not_cooldown_or_rotate(dual_repo):
    """上游 400（模型不存在等）→ 抛 InvalidRequest；凭证不冷却、不轮换。"""
    from src.compat.openai.request import InvalidRequest

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"code": 1002, "msg": "model not found"})

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    provider = CodeBuddyProvider(client=_client(handler))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo, scheduler=Scheduler(),
        default_model="qwen3.8"))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8"), username="u")
    assert "qwen3.8" in str(exc_info.value)
    assert calls["n"] == 1                      # 未轮换重试
    # 凭证零错误记录、无冷却、未禁用
    row = db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials").fetchone()
    assert tuple(row) == (0, None, 0)

async def test_http_400_stream_yields_invalid_request_frame(dual_repo):
    """流式路径：400 → invalid_request 错误帧；凭证不冷却、不轮换。"""
    import json as _json

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 1002, "msg": "model not found"})

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    provider = CodeBuddyProvider(client=_client(handler))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo, scheduler=Scheduler(),
        default_model="qwen3.8"))

    frames = [f async for f in executor.stream(_request("qwen3.8"), username="u")]
    assert len(frames) == 1                     # 错误帧（内含 [DONE]）
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["code"] == "invalid_request"
    assert "qwen3.8" in payload["error"]["message"]
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert tuple(row) == (0, None)

async def test_invalid_skips_provider_and_falls_back_to_next(dual_repo):
    """CB 400（不认识模型）→ 跳过 CB，TRAE 成功接住；凭证零冷却。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # pin CB 保证它先被选中，从而触发「400 → 跳过 → TRAE 接住」路径
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [_http_400()])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and trae.calls == 1
    assert tuple(db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials").fetchone()) \
        is not None
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials")]
    assert rows == [(0, None, 0), (0, None, 0)]     # 谁都没被冷却

async def test_all_upstreams_reject_yields_400(dual_repo):
    """两个上游都 400 → InvalidRequest（400 语义），凭证零冷却。"""
    from src.compat.openai.request import InvalidRequest

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8-max"), username="u")
    assert "qwen3.8-max" in str(exc_info.value)
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials")]
    assert rows == [(0, None, 0), (0, None, 0)]

async def test_flat_exclusive_model_skips_other_upstream(dual_repo):
    """CodeBuddy 独有模型（目录可证归属）→ 只打 CB；TRAE 零调用、零统计记录。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # pin TRAE：旧逻辑会先选中它并把请求真实打到 TRAE（留下无效请求记录）
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    records: list[dict] = []

    class _Stats:
        @staticmethod
        def record(**fields):
            records.append(fields)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="cb-only", stats=_Stats(),
        # "ghost" 未注册：顺带覆盖目录里混入未知上游的过滤分支
        model_aliases={"codebuddy": {"cb-only": "cb-only"},
                       "ghost": {"cb-only": "cb-only"}}))

    result = await executor.complete(_request("cb-only"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 0 and cb.calls == 1
    assert [(r["provider"], r["ok"]) for r in records] == [("codebuddy", True)]

async def test_free_tier_preferred_over_paid_when_catalog_has_rates(dual_repo):
    """候选中存在免费渠道（credit_rate=0）→ 免费渠道优先，不烧付费积分。

    实测故障（2026-10-08）：space-bunny 由 zen（`space-bunny-free`，x0）与
    CodeBuddy（`space-bunny`，x0.08）同时登记，合并成一条后调度按「到期
    积分多者先用」选中 CodeBuddy，免费 zen 被晾着——付费渠道的 36h 到期
    积分永远赢过免费渠道。现在 executor 把模型目录里的倍率注进候选，
    候选里出现免费渠道时它整体排前。
    """
    from src.provider.base import Model

    repo, _db = dual_repo
    # pin CB 且带足到期积分：旧排序必选 CB，证明免费档压过 pin 之外的
    # 一切既有指标（pin 本身仍是用户显式意图，最高优先）
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="zen", credential_data={})
    cb = _RejectProvider("codebuddy", [GOOD])
    zen = _RejectProvider("zen", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "zen": zen}, credentials=repo,
        scheduler=Scheduler(), default_model="space-bunny",
        model_aliases={"codebuddy": {"space-bunny": "space-bunny"},
                       "zen": {"space-bunny": "space-bunny-free"}},
        model_list_cache={
            "codebuddy": {"space-bunny": Model(id="space-bunny",
                                               credit_rate=0.08)},
            "zen": {"space-bunny-free": Model(id="space-bunny-free",
                                              credit_rate=0.0)}}))

    result = await executor.complete(_request("space-bunny"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (cb.calls, zen.calls) == (0, 1)

async def test_free_tier_unknown_rate_keeps_legacy_order(dual_repo):
    """目录未就绪 / 倍率未知 → 不启用免费档，保持原排序（向后兼容）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="zen", credential_data={})
    # zen pinned：旧排序 pin 优先。目录有别名但没有 zen 的倍率条目
    # （该渠道模型列表回退静态表丢掉该模型）→ zen 与付费同档
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'zen'")
    cb = _RejectProvider("codebuddy", [GOOD])
    zen = _RejectProvider("zen", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "zen": zen}, credentials=repo,
        scheduler=Scheduler(), default_model="space-bunny",
        model_aliases={"codebuddy": {"space-bunny": "space-bunny"},
                       "zen": {"space-bunny": "space-bunny-free"}},
        model_list_cache={"codebuddy": {"space-bunny": Model(
            id="space-bunny", credit_rate=0.08)}}))

    result = await executor.complete(_request("space-bunny"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (cb.calls, zen.calls) == (0, 1)

async def test_free_tier_ignored_without_catalog(dual_repo):
    """ExecutorDeps.model_list_cache 缺省（None）→ 完全走原行为（老测试不破）。"""
    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="zen", credential_data={})
    cb = _RejectProvider("codebuddy", [GOOD])
    zen = _RejectProvider("zen", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "zen": zen}, credentials=repo,
        scheduler=Scheduler(), default_model="space-bunny",
        model_aliases={"codebuddy": {"space-bunny": "space-bunny"},
                       "zen": {"space-bunny": "space-bunny-free"}}))

    result = await executor.complete(_request("space-bunny"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls + zen.calls == 1

async def test_forced_provider_bypasses_catalog_narrowing(dual_repo):
    """@provider 强制指定不过滤：目录里只有 CB 登记也照样打 TRAE。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="cb-only",
        model_aliases={"codebuddy": {"cb-only": "cb-only"}}))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("cb-only@trae"), username="u")
    assert trae.calls == 1 and cb.calls == 0

async def test_narrowing_disabled_without_catalog(dual_repo):
    """目录未就绪（model_aliases 缺省/为空）→ 保持双上游参与的原行为。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1

async def test_narrowing_keeps_candidates_when_model_unknown_to_catalog(dual_repo):
    """目录就绪但没有已注册上游登记该模型 → 不过滤（黑名单滤掉仍可直连）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max",
        model_aliases={"codebuddy": {"glm-5.2": "glm-5.2"},
                       "trae": {"GLM-5.2": "GLM-5.2"}}))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1

async def test_narrowed_provider_without_usable_credential_does_not_fan_out(dual_repo):
    """收窄渠道无可用凭证 → 不扇出到未登记该模型的渠道（只信模型目录）。

    实测故障（2026-10-10 `longcat-2.5-preview`）：目录里只有 zen 登记它
    （原代号 `longcat-2.5-preview-free`），zen 匿名免费层 429 让虚拟凭证进
    60s 账号级冷却后，`_select` 旧逻辑放宽到全部渠道，CB/TRAE/kilo/CodeArts
    各回 11102/4001/401/404——客户端拿到误导性的 400 而非「该渠道暂时
    不可用」，还给 CB 写下 6 小时起步的 (凭证, 模型) 负缓存。现在直接出
    503，其余渠道零调用。此用例即该场景的合成版（唯一登记渠道被硬禁用）。
    """
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # CodeBuddy 有凭证但被硬禁用：收窄到它之后没有任何可用候选
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="deepseek-v4.1-flash",
        # 陈旧/降级目录：只登记了 CodeBuddy，TRAE 被漏掉
        model_aliases={"codebuddy": {"deepseek-v4.1-flash": "deepseek-v4.1-flash"}}))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request("deepseek-v4.1-flash"), username="u")
    assert (cb.calls, trae.calls) == (0, 0)

async def test_model_cooling_exhaustion_does_not_fan_out(dual_repo):
    """收窄渠道全部因**模型级冷却**不可选 → 不放宽重试（不扇出到不认的渠道）。

    实测故障（2026-10-08 15:53）：`qwen3.8-flash` 只有 Qoder 登记（内部原代号
    `qfmodel`），Qoder 节点故障（`[FAIL]node:oa_qwen-plus-main Execution failed`，
    按 Q50 归 MODEL）让两条 Qoder 凭证进 600s 模型级冷却；此后每次请求收窄到
    `{qoder}` → 空 → 触发 broader 回退，把请求甩给不持有该模型的渠道——它们的
    别名表没有这个键、拿到的仍是原始名 `qwen3.8-flash`，必然被拒（CB 11102 /
    TRAE 4001 / zen·kilo 401），实测 6 次轮换全废、还给 CB 写下 6 小时起步的
    BLOCKED 负缓存。现在此路径不扇出，直接给模型级 503（换个模型即可用）。
    """
    repo, db = dual_repo
    qoder_a = repo.add(provider="qoder", credential_data={"accessToken": "a"})
    qoder_b = repo.add(provider="qoder", credential_data={"accessToken": "b"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # 冷却登记用**上游原始名**（冷却表按各渠道自己调用的名字）
    cool = ErrorOutcome(model_cooldowns={"qfmodel": ModelCooldown(
        cooling_until=int(time.time()) + 600, hits=1, reason="model")})
    repo.save_error(qoder_a, cool)
    repo.save_error(qoder_b, cool)
    qoder = _RejectProvider("qoder", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"qoder": qoder, "codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-flash",
        # 只有 Qoder 登记该模型（内部原代号 qfmodel）
        model_aliases={"qoder": {"qwen3.8-flash": "qfmodel"}},
        # 与 main.py 同款注入：模型冷却按各渠道实际调用的名字（qfmodel）对齐
        upstream_model_name=lambda pid, name: {
            "qoder": {"qwen3.8-flash": "qfmodel"}}.get(pid, {}).get(name.lower(), name),
        model_suggestions=lambda _name: ["qwen3.8-max"]))

    with pytest.raises(NoHealthyCredential) as exc_info:
        await executor.complete(_request("qwen3.8-flash"), username="u")
    assert "temporarily unavailable on upstream" in str(exc_info.value)
    assert "qwen3.8-max" in str(exc_info.value)
    assert (qoder.calls, cb.calls, trae.calls) == (0, 0, 0)
    # 负缓存零新增：没有渠道被写「该后端无此模型」
    assert db.connect().execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 2

async def test_model_cooled_registered_peer_takes_over(dual_repo):
    """收窄渠道模型级冷却、但另一条**已登记**渠道有可用凭证 → 走登记渠道。

    非扇出路径：目录同时登记了 qoder 与 codebuddy，qoder 被模型级冷却后
    收窄候选池里 codebuddy 本身就是可用候选，无需放宽到任何未登记渠道。
    """
    repo, db = dual_repo
    qoder_ok = repo.add(provider="qoder", credential_data={"accessToken": "a"})
    dead = repo.add(provider="qoder", credential_data={"accessToken": "b"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    # 一条账号级硬禁用 + 一条模型级冷却：混有成因也不放宽（登记渠道已够用）
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (dead,))
    repo.save_error(qoder_ok, ErrorOutcome(model_cooldowns={"qfmodel": ModelCooldown(
        cooling_until=int(time.time()) + 600, hits=1, reason="model")}))
    qoder = _RejectProvider("qoder", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"qoder": qoder, "codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-flash",
        # 目录：qoder 与 codebuddy 都登记了该模型，qoder 又不可用 → 回落 CB
        model_aliases={"qoder": {"qwen3.8-flash": "qfmodel"},
                       "codebuddy": {"qwen3.8-flash": "qwen3.8-flash"}},
        upstream_model_name=lambda pid, name: {
            "qoder": {"qwen3.8-flash": "qfmodel"}}.get(pid, {}).get(name.lower(), name)))

    result = await executor.complete(_request("qwen3.8-flash"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (qoder.calls, cb.calls) == (0, 1)

async def test_narrowed_channel_without_credentials_does_not_broaden(dual_repo):
    """收窄渠道**一条凭证都没有** → 不放宽到未登记渠道（无候选 ≠ 该试别的）。

    目录说该模型只在 codearts，而 codearts 一条凭证都没有：直接出 503，
    不去打没登记它的 codebuddy——那只会拿 11102/4001 换一条误导性错误。
    """
    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "codearts": _RejectProvider("codearts", [GOOD])},
        credentials=repo, scheduler=Scheduler(), default_model="m-only",
        # 只有 codearts 登记，但该渠道无凭证 → 不放宽
        model_aliases={"codearts": {"m-only": "m-only"}}))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request("m-only"), username="u")
    assert cb.calls == 0

async def test_owner_channel_account_cooled_does_not_fan_out(dual_repo):
    """持有渠道账号级冷却（429）→ 不扇出到未登记渠道（2026-10-10 longcat 实测）。

    `longcat-2.5-preview` 在模型目录里只有 zen 登记（原代号
    `longcat-2.5-preview-free`）。zen 匿名免费层 429（kind=SOFT）让虚拟凭证
    进 60s 账号级冷却后，旧逻辑每次请求都放宽打 CB/TRAE/kilo/CodeArts——各回
    11102/4001/401/404，客户端拿到 400 `invalid_request` 而非「zen 暂时
    不可用」，还给 CB 写下 6 小时起步的负缓存（线上实测 usage_events 里同一
    模型同时出现 kilo/codearts/codebuddy/trae 的失败记录）。现在只信目录：
    zen 冷却中则零上游调用，直接出 503。
    """
    repo, db = dual_repo
    zen_id = repo.add(provider="zen", credential_data={})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    repo.add(provider="kilo", credential_data={})
    # zen 被 429 打冷却（账号级 SOFT）：目录里唯一登记该模型的渠道不可用
    repo.save_error(zen_id, ErrorOutcome(cooling_until=int(time.time()) + 60,
                                         err_count=0))
    zen = _RejectProvider("zen", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    kilo = _RejectProvider("kilo", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"zen": zen, "codebuddy": cb, "trae": trae, "kilo": kilo},
        credentials=repo, scheduler=Scheduler(), default_model="longcat-2.5-preview",
        model_aliases={"zen": {"longcat-2.5-preview": "longcat-2.5-preview-free"}},
        upstream_model_name=lambda pid, name: {
            "zen": {"longcat-2.5-preview": "longcat-2.5-preview-free"}}
        .get(pid, {}).get(name.lower(), name)))

    with pytest.raises(NoHealthyCredential) as exc_info:
        await executor.complete(_request("longcat-2.5-preview"), username="u")
    assert "all credentials unavailable" in str(exc_info.value)
    assert (zen.calls, cb.calls, trae.calls, kilo.calls) == (0, 0, 0, 0)
    # 零负缓存：没有任何渠道被写「该后端无此模型」
    assert db.connect().execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 0

async def test_forced_provider_without_usable_credential_keeps_narrowing(dual_repo):
    """@provider 强制指定：该渠道无可用凭证也不回退（强制语义不被兜底削弱）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="deepseek-v4.1-flash",
        model_aliases={"codebuddy": {"deepseek-v4.1-flash": "deepseek-v4.1-flash"},
                       "trae": {"deepseek-v4.1-flash": "deepseek-v4.1-flash"}}))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request("deepseek-v4.1-flash@codebuddy"), username="u")
    assert (cb.calls, trae.calls) == (0, 0)

async def test_stream_rotate_exhausted_with_invalid_last_error(dual_repo):
    """流式 + 轮换耗尽时最后错误是 INVALID → invalid_request 帧（155-159）。"""
    import json as _json

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen3.8"))

    frames = [f async for f in executor.stream(_request("qwen3.8"), username="u")]
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["code"] == "invalid_request"
    assert "qwen3.8" in payload["error"]["message"]

async def test_complete_rotate_exhausted_with_invalid_last_error(dual_repo):
    """非流式 + 轮换耗尽 + 最后错误 INVALID → 400（executor 238）。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen3.8"))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("qwen3.8"), username="u")

async def test_executor_invalid_request_includes_suggestions(dual_repo):
    """全部上游 400 → 400 文案附相近模型建议。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "tr"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": _RejectProvider("codebuddy", [_http_400()]),
                   "trae": _RejectProvider("trae", [_http_400()])},
        credentials=repo, scheduler=Scheduler(), default_model="qwen3.8-max",
        model_suggestions=lambda name: ["qwen-3.7-plus"]))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8-max"), username="u")
    assert "qwen-3.7-plus" in str(exc_info.value)

async def test_chat_pacer_throttles_and_can_be_disabled():
    """CB 聊天节流：请求前等待；None/关闭时不等待。"""
    import time as _time

    from src.provider.codebuddy.client import CodeBuddyProvider

    waited = []

    class FakePacer:
        async def wait_turn(self, key=None):
            waited.append((key, _time.monotonic()))

        def release(self, key=None):
            pass

    async def stream_ok(cred, payload, model):
        yield GOOD[0]

    class FakeClient:
        async def stream_chat(self, cred, payload, model):
            async for ev in stream_ok(cred, payload, model):
                yield ev

    provider = CodeBuddyProvider(client=FakeClient(), pacer=FakePacer())
    events = [e async for e in provider.stream_chat(
        {"bearer_token": "t", "account_uid": "acct-1"}, {}, "m")]
    assert events and waited
    # 桶键按凭证身份派生，而不是空键
    assert waited[0][0] == stable_key("codebuddy", "acct-1")

    provider2 = CodeBuddyProvider(client=FakeClient(), pacer=None)
    waited.clear()
    _ = [e async for e in provider2.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert not waited

async def test_stream_inner_error_other_is_logged_and_counted(dual_repo, caplog):
    """流内非 4001/1005 错误 → 记冷却并打日志（可观测性回归）。"""
    import logging

    from src.provider.base import Event

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    calls = {"n": 0}

    async def gen(_cred, _payload, _model):
        calls["n"] += 1
        yield Event(kind=EventKind.ERROR, error_code=41291, error_message="rate limited")

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="m"))
    frames = [f async for f in executor.stream(_request("m"), username="u")]
    assert any(b"error" in f for f in frames)
    assert calls["n"] == 1
    assert any("流内错误" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)

async def test_stream_inner_4001_yields_invalid_frame(dual_repo, caplog):
    """流内 4001 → INVALID：跳过上游 + invalid_request 帧。"""
    import json as _json
    import logging

    from src.provider.base import Event

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    calls = {"n": 0}

    async def gen(_cred, _payload, _model):
        calls["n"] += 1
        yield Event(kind=EventKind.ERROR, error_code=4001, error_message="param invalid",
                    error_kind=ErrKind.INVALID)

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="m",
        model_suggestions=lambda _n: ["alt-model"]))

    with caplog.at_level(logging.WARNING):
        frames = [f async for f in executor.stream(_request("m"), username="u")]
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["code"] == "invalid_request"
    assert "alt-model" in payload["error"]["message"]
    assert calls["n"] == 1                              # 不轮换
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert tuple(row) == (0, None)                      # 不冷却
    assert any("流内拒绝模型" in r.getMessage() for r in caplog.records)

async def test_stream_chat_body_carries_cli_signature_fields():
    """官方 CLI 特征字段：enable_thinking / reasoning_effort / include_usage。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [{"role": "user", "content": "hi"}],
               "stream_options": {"include_usage": False}}
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    body = captured["body"]
    assert body["enable_thinking"] is True          # 缺失触发 11128 渠道风控
    assert body["reasoning_effort"] == "medium"     # 缺失则思考被写进正文
    assert body["stream_options"]["include_usage"] is True
    assert body["stream"] is True
    assert body["model"] == "m"
    assert events[-1].kind is EventKind.FINISH

async def test_stream_chat_keeps_client_reasoning_effort():
    """客户端显式给的 effort 不被覆盖：补缺省不改写客户端意图。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [{"role": "user", "content": "hi"}],
               "reasoning_effort": "low"}
    [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    assert captured["body"]["reasoning_effort"] == "low"

async def test_trae_4001_falls_through_to_codebuddy(dual_repo, caplog):
    """TRAE 流内 4001 → 跳过 TRAE → CB 成功接住；TRAE 凭证不被冷却。"""

    from src.provider.base import Event

    class Trae4001:
        id = "trae"
        calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            yield Event(kind=EventKind.ERROR, error_code=4001, error_message="param",
                        error_kind=ErrKind.INVALID)

    class CbOk:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "tr"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    trae, cb = Trae4001(), CbOk()
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(), default_model="m"))

    result = await executor.complete(_request("m"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials ORDER BY provider")]
    assert rows == [(0, None), (0, None)]       # TRAE 4001 不冷却

async def test_complete_timeout_rotates_to_next_credential(dual_repo):
    """非流式聚合超时：按瞬态错误（SOFT）换号重试，请求不悬挂。"""
    import asyncio

    class SlowFirst:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls > 1:
                yield Event(kind=EventKind.CONTENT, content="ok")
                yield Event(kind=EventKind.FINISH, finish_reason="stop")
                return
            yield Event(kind=EventKind.CONTENT, content="x")
            await asyncio.Event().wait()    # 首次调用模拟半开停滞的连接

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    slow = SlowFirst()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": slow}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_timeout_seconds=0.05))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert slow.calls == 2
    # 超时按 SOFT 处理：短冷却，不累计 err_count（不罚死号）
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count FROM credentials ORDER BY id")]
    assert rows == [(0,), (0,)]

async def test_complete_timeout_without_rotation_raises_unavailable(dual_repo):
    """无号可换时聚合超时以 503 语义收尾（NoHealthyCredential）。"""
    import asyncio

    class AlwaysSlow:
        async def stream_chat(self, _cred, _payload, _model):
            yield Event(kind=EventKind.CONTENT, content="x")
            await asyncio.Event().wait()

    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": AlwaysSlow()}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_timeout_seconds=0.05))

    # M2：对外只给受控标识（异常类名），不回显内部错误正文；完整原因在服务端日志
    with pytest.raises(NoHealthyCredential, match="UpstreamStreamError"):
        await executor.complete(_request(), username="u")

async def test_complete_timeout_zero_disables_backstop(dual_repo):
    """≤0 关闭兜底：请求正常完成（覆盖关闭分支）。"""
    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": DualProvider("codebuddy", [GOOD])},
        credentials=repo, scheduler=Scheduler(), default_model="glm-5.2",
        complete_timeout_seconds=0))
    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"

async def test_complete_max_events_overflow_rotates(dual_repo):
    """非流式聚合事件数超上限：按瞬态错误换号，不无界增长（M10）。"""
    class EndlessFirst:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls > 1:
                yield Event(kind=EventKind.CONTENT, content="ok")
                yield Event(kind=EventKind.FINISH, finish_reason="stop")
                return
            # 首个凭证持续吐 chunk 且不结束（模拟死循环输出）
            for _ in range(50):
                yield Event(kind=EventKind.CONTENT, content="x")

    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    endless = EndlessFirst()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": endless}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_max_events=2))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert endless.calls == 2

async def test_complete_max_events_zero_disables(dual_repo):
    """≤0 关闭上限：事件再多也照常聚合（覆盖关闭分支）。"""
    class ManyEvents:
        async def stream_chat(self, _cred, _payload, _model):
            for _ in range(10):
                yield Event(kind=EventKind.CONTENT, content="x")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": ManyEvents()}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_max_events=0))
    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "x" * 10

async def test_stream_chat_normalizes_developer_role():
    """PI 的 developer 角色归一为 system（腾讯实测 developer → 11128）。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [
        {"role": "developer", "content": "You are PI."},
        {"role": "user", "content": "hi"},
    ]}
    _ = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "user"]

async def test_stream_chat_mutations_do_not_leak_into_payload():
    """出站改写（developer→system / 指纹中和 / 脏 tool_call 清理）绝不穿透回
    payload：executor 的会话粘性在改写前后都用请求原文算指纹（pin_for /
    remember），穿透会让指纹链失配、粘性静默失效。"""
    import copy

    from src.provider.codebuddy.client import CHANNEL_MARKERS

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [
        {"role": "developer", "content": f"plan. {CHANNEL_MARKERS[0]}"},
        {"role": "assistant", "content": [{"type": "text",
                                           "text": CHANNEL_MARKERS[1]}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function", "function": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x"},
        {"role": "user", "content": "hi"},
    ]}
    snapshot = copy.deepcopy(payload)
    _ = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    assert payload == snapshot
    # 出站体确实发生了全部三类改写（测试有效性自证）
    outbound = json.dumps(captured["body"], ensure_ascii=False)
    assert captured["body"]["messages"][0]["role"] == "system"
    assert CHANNEL_MARKERS[0] not in outbound and CHANNEL_MARKERS[1] not in outbound
    assert len(captured["body"]["messages"]) == 3    # 脏 assistant 与悬空 tool 被清

async def test_stream_chat_sanitizes_markers_and_warns(caplog):
    """默认开启：出站正文被中和并告警；可关。"""
    import json as _json

    marker = "Main branch (you will usually use this for PRs):"

    def fresh_payload() -> dict:
        return {"messages": [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": f"见 {marker}"},
            {"role": "user", "content": "hi"},
        ]}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        capture_handler.body = _json.loads(request.read())  # type: ignore[attr-defined]
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    with caplog.at_level(logging.WARNING):
        [e async for e in _client(capture_handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), fresh_payload(), "m")]
        body = capture_handler.body  # type: ignore[attr-defined]
    assert body["messages"][1]["content"] == "见 [external-client-identity]"
    assert body["messages"][2]["content"] == "hi"     # user 原样
    assert any("伪装客户端指纹" in r.getMessage() for r in caplog.records)

    def passthrough_handler(request: httpx.Request) -> httpx.Response:
        passthrough_handler.body = _json.loads(request.read())  # type: ignore[attr-defined]
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    client = _client(passthrough_handler)
    client.sanitize_markers = False
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        [e async for e in client.stream_chat(
            CodeBuddyCredential(bearer_token="t"), fresh_payload(), "m")]
        body = passthrough_handler.body  # type: ignore[attr-defined]
    assert body["messages"][1]["content"] == f"见 {marker}"   # 关闭后透传
    assert not any("伪装客户端指纹" in r.getMessage() for r in caplog.records)

async def test_stream_6004_cools_only_that_model(dual_repo):
    """429 + 6004：只写 (凭证, 模型) 条目，账号级冷却保持为空。"""
    from src.provider.base import Event

    repo, db = dual_repo
    cred_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    async def gen(_cred, _payload, _model):
        yield Event(kind=EventKind.ERROR, error_code=6004, error_message="model quota",
                    error_kind=ErrKind.MODEL)

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"error" in f for f in frames)

    account = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchone()
    assert tuple(account) == (None, 0)                   # 账号未被冷却
    assert _model_cooldown_rows(db) == [(cred_id, "glm-5.2", 1, "model")]

async def test_stream_11102_blocks_account_model_pair(dual_repo):
    """11102「该后端无此模型」→ negative cache，且换号能继续服务。"""
    from src.provider.base import Event

    repo, db = dual_repo
    blocked_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    healthy_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "h"})
    # 让被负缓存的凭证先被选中（pin 优先）
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (blocked_id,))
    db.connect().commit()

    class P:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, cred, _payload, _model):
            self.calls += 1
            if cred["bearer_token"] == "b":
                yield Event(kind=EventKind.ERROR, error_code=11102,
                            error_message="no such model", error_kind=ErrKind.BLOCKED)
                return
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    provider = P()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo,
        scheduler=Scheduler(), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"ok" in f for f in frames)                # 第二个凭证接住
    assert _model_cooldown_rows(db) == [(blocked_id, "glm-5.2", 1, "blocked")]
    # 该凭证的其他模型不受影响
    assert repo.candidates()[0].model_cooldowns is not None
    assert repo.candidates()[1].model_cooldowns is None
    assert healthy_id != blocked_id

async def test_stream_11101_request_error_does_not_touch_credential(dual_repo):
    """请求级错误（11101 请求体坏）：不冷却、不累计，但仍换号重试。

    err_count 预置为 2（阈值 3）：若请求级错误被当成普通错误累计，
    第三次就会触发熔断冷却，健康凭证被踢出池——这正是要防的误伤。
    """
    from src.engine.scheduler import ErrorOutcome
    from src.provider.base import Event

    repo, db = dual_repo
    for token in ("a", "b", "c"):
        cred_id = repo.add(provider="codebuddy", credential_data={"bearer_token": token})
        repo.save_error(cred_id, ErrorOutcome(err_count=2))

    calls = {"n": 0}

    class P:
        id = "codebuddy"

        async def stream_chat(self, _cred, _payload, _model):
            calls["n"] += 1
            yield Event(kind=EventKind.ERROR, error_code=11101,
                        error_message="Unmarshal chat params failed",
                        error_kind=ErrKind.REQUEST)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=3), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"error" in f for f in frames)
    assert calls["n"] == 3                               # 换号重试到上限，不是原地放弃

    rows = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchall()
    assert [tuple(row) for row in rows] == [(None, 2)] * 3   # 原样保留：没冷却也没累计
    assert _model_cooldown_rows(db) == []

async def test_http_400_11128_rotates_instead_of_skipping_provider(dual_repo):
    """渠道风控 11128（400）→ REQUEST：换号重试，不是跳过上游直接 400。

    修复前 11128 落 INVALID：_skip_provider 把该上游全部凭证塞进 tried，
    should_rotate 立即为假，第一条请求就以 invalid_request 400 收尾（零重试）。
    回归证据：真实日志里同凭证同时刻换模型即成功、同 (凭证, 模型) 秒级交替
    成功/失败，属于瞬时风控而非模型/凭证问题。
    """
    repo, db = dual_repo
    first = repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (first,))

    class P:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                raise UpstreamHTTPError(400, _CB_11128_BODY)
            for event in GOOD:
                yield event

    provider = P()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo,
        scheduler=Scheduler(max_rotate=3), default_model="glm-5.2"))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2                     # 换到第二个凭证重试成功

    rows = db.connect().execute(
        "SELECT cooling_until, err_count, disabled FROM credentials").fetchall()
    assert [tuple(row) for row in rows] == [(None, 0, 0)] * 2   # 两个凭证都零惩罚
    assert _model_cooldown_rows(db) == []

async def test_complete_402_cools_until_next_credit_reset(dual_repo):
    """402 余额不足：非流式路径冷到次日签到时刻（不是固定 12h）。"""
    from src.engine.scheduler import next_credit_reset

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    class PaymentRequired(Exception):
        def kind(self):
            return ErrKind.CREDIT

    executor, _trae, codebuddy = _dual_executor(repo, GOOD, [[PaymentRequired()]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())

    cooling_until = db.connect().execute(
        "SELECT cooling_until FROM credentials").fetchone()["cooling_until"]
    assert cooling_until == next_credit_reset(int(time.time()))
