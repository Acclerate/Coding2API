"""非流式 fallback：链回退、成员去重、错误优先级与单例语义。"""

from __future__ import annotations

import pytest

from src.compat.openai.request import InvalidRequest, parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps
from src.engine.model_resolver import ModelTarget
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Event, EventKind
from tests.helpers.constants import SECRET

GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]

INVALID = [Event(kind=EventKind.ERROR, error_code="11102",
                 error_message="service info not found", error_kind=ErrKind.INVALID)]

TRANSIENT = [Event(kind=EventKind.ERROR, error_code="500",
                   error_message="boom", error_kind=ErrKind.OTHER)]

@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "fallback.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

class ScriptedProvider:
    """每次调用按顺序回放 `script` 里的事件列表（越界后重复最后一个）。"""

    def __init__(self, provider_id: str, script: list[list[Event]] | None = None) -> None:
        self.id = provider_id
        self.script = script or [GOOD]
        self.calls = 0
        self.models_seen: list[str] = []

    async def stream_chat(self, _credential_data, _payload, model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        self.models_seen.append(model)
        for event in self.script[index]:
            yield event

    def list_models(self, _credential_data):
        return []

def _add(credentials, provider: str, token: str) -> None:
    credentials.add(provider=provider, credential_data={"bearer_token": token})

def _request(model="glm-5.2"):
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": model})

def _aliases(primary_model: str, fallback_model: str) -> dict[str, dict[str, str]]:
    return {"codebuddy": {primary_model.lower(): primary_model},
            "trae": {fallback_model.lower(): fallback_model}}

def _executor(providers, credentials, groups, aliases=None, **scheduler_kw):
    return Executor(ExecutorDeps(
        providers=providers, credentials=credentials,
        scheduler=Scheduler(**scheduler_kw), default_model="glm-5.2",
        model_aliases=aliases or {}, fallback_groups=groups))

class ModelAwareProvider:
    """只接受白名单里的模型，其余回 INVALID（贴近真实上游）。"""

    def __init__(self, provider_id: str, good_models: set[str]) -> None:
        self.id = provider_id
        self.good_models = {m.lower() for m in good_models}
        self.calls = 0
        self.models_seen: list[str] = []

    async def stream_chat(self, _credential_data, _payload, model):
        self.calls += 1
        self.models_seen.append(model)
        for event in (GOOD if model.lower() in self.good_models else INVALID):
            yield event

    def list_models(self, _credential_data):
        return []

@pytest.mark.asyncio
async def test_complete_falls_back_when_primary_all_invalid(repo):
    """主模型（组内成员）全 INVALID → 回退到下一成员。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and tr.calls == 1
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_group_name_entry(repo):
    """请求兼容组名：以组内首成员发起。"""
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"trae": tr}, credentials,
                         {"fast": ("glm-4.6", "glm-5")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    result = await executor.complete(_request("fast"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_skips_member_missing_from_catalog(repo):
    """目录确认成员不挂在任何候选渠道 → 跳过，不白打上游。"""
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"trae": tr}, credentials,
                         {"fast": ("ghost-model", "glm-4.6")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    result = await executor.complete(_request("fast"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_dedupes_repeated_member(repo):
    """组配置写重了不重复尝试。"""
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"trae": tr}, credentials,
                         {"fast": ("glm-4.6", "glm-4.6")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    result = await executor.complete(_request("fast"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_raises_last_error_when_chain_exhausted(repo):
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [INVALID])
    executor = _executor({"codebuddy": cb}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         {"codebuddy": {"glm-5.2": "glm-5.2"}})
    with pytest.raises(InvalidRequest):
        await executor.complete(_request("glm-5.2"))
    db.close()

@pytest.mark.asyncio
async def test_complete_no_fallback_when_groups_empty(repo):
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [GOOD])
    executor = _executor({"codebuddy": cb}, credentials, {})
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    db.close()

@pytest.mark.asyncio
async def test_complete_no_fallback_when_model_not_in_group(repo):
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [GOOD])
    executor = _executor({"codebuddy": cb}, credentials, {"fast": ("glm-4.6",)})
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    db.close()

@pytest.mark.asyncio
async def test_forced_provider_never_falls_back(repo):
    """`@渠道` 把渠道钉死，用户明确意图：不参与跨渠道回退。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    with pytest.raises(InvalidRequest):
        await executor.complete(_request("glm-5.2@codebuddy"))
    assert tr.calls == 0
    db.close()

def test_fallback_chain_skips_unknown_provider_member(repo):
    """兼容组成员写了未知渠道：跳过该成员，不因此丢掉整条链。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("a", "b@nosuchchannel")})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["a"]
    db.close()

def test_fallback_chain_empty_when_all_members_unknown(repo):
    """所有成员都写了未知渠道：退化为请求模型本身，交给执行层报 400。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("a@nope",)})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["fast"]
    db.close()

def test_fallback_chain_filters_pure_alias_group(repo):
    """组名是纯别名（不在目录里）→ 不进链；成员命中则用之。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("glm-4.6",)},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["glm-4.6"]
    db.close()

def test_fallback_chain_all_members_filtered_returns_alias(repo):
    """组名别名 + 所有成员都不在目录：退化为请求模型本身。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("ghost",)},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["fast"]
    db.close()

@pytest.mark.asyncio
async def test_fallback_with_multiple_primary_credentials(repo):
    """主模型多凭证依次软失败后仍能回退（每链项各自 3 次预算）。"""
    credentials, db = repo
    for token in ("cb1", "cb2", "cb3"):
        _add(credentials, "codebuddy", token)
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [TRANSIENT])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 3 and tr.calls == 1
    db.close()

@pytest.mark.asyncio
async def test_complete_503_primary_then_fallback_ok(repo):
    """主模型无可用凭证（唯一登记渠道无凭证）→ 回退链尝试下一项。

    同流式路径：不在未登记 `glm-5.2` 的 trae 上白打一次（旧逻辑会先拿原始名
    打过去、拿一个 INVALID 再回退），直接从链首的「无可用凭证」进下一成员。
    """
    credentials, db = repo
    # 只有 trae 有凭证；codebuddy 一条都没有
    _add(credentials, "trae", "tr")
    tr = ModelAwareProvider("trae", {"glm-4.6"})
    executor = _executor({"codebuddy": ScriptedProvider("codebuddy"),
                          "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_all_members_skipped_raises_primary_error(repo):
    """回退成员都被目录过滤：以链首错误收尾。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [INVALID])
    executor = _executor({"codebuddy": cb, "trae": ScriptedProvider("trae")},
                         credentials, {"chat": ("glm-5.2", "ghost")},
                         {"codebuddy": {"glm-5.2": "glm-5.2"}})
    with pytest.raises(InvalidRequest):
        await executor.complete(_request("glm-5.2"))
    db.close()

def test_with_model_returns_same_instance_when_unchanged(repo):
    credentials, db = repo
    executor = _executor({}, credentials, {})
    request = _request("glm-5.2")
    assert executor._with_model(request, "glm-5.2") is request
    swapped = executor._with_model(request, "glm-4.6")
    assert swapped.model == "glm-4.6" and swapped.raw["model"] == "glm-4.6"
    db.close()

def test_fallback_allowed_without_catalog(repo):
    """目录未就绪（aliases 空）时放行回退，交给执行层兜底候选。"""
    credentials, db = repo
    executor = _executor({}, credentials, {})
    assert executor._fallback_allowed(ModelTarget(model="anything", providers=("trae",)))
    db.close()
