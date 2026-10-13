"""执行引擎：统计埋点、错误分类、选号与轮换。"""

from __future__ import annotations

import time

import pytest

from src.compat.openai.request import parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import ApiKeyRepository, CredentialRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential, NoProviderForModel
from src.engine.scheduler import Scheduler
from src.engine.sse import SSE_DONE
from src.provider.base import ErrKind, Event, EventKind, Model, Quota, Usage
from src.provider.trae.events import UpstreamProtocolViolation
from tests.helpers.constants import SECRET


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

def _disabled_outcome():
    from src.engine.scheduler import Candidate, Scheduler

    return Scheduler().note_error(Candidate(credential_id="c", provider="codebuddy"),
                                  ErrKind.DEAD, 0)

class RecordingCollector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.boom = False

    def record(self, **fields) -> None:
        if self.boom:
            raise RuntimeError("stats backend down")
        self.events.append(fields)

def _exec_with_stats(repo_tuple, script, collector, **kw):
    credentials, _db = repo_tuple
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    class Streaming:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _data, _payload, _model):
            index = min(self.calls, len(script) - 1)
            self.calls += 1
            for item in script[index]:
                if isinstance(item, Exception):
                    raise item
                yield item

        def list_models(self, _data):  # pragma: no cover
            return []

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Streaming()}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2", stats=collector))
    return executor

GOOD_EVENTS = [Event(kind=EventKind.CONTENT, content="hi"),
               Event(kind=EventKind.USAGE, usage=Usage(11, 22, 3)),
               Event(kind=EventKind.FINISH, finish_reason="stop")]

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

class Boom(Exception):
    def __init__(self, kind: ErrKind, message: str = "upstream error") -> None:
        super().__init__(message)
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind

@pytest.fixture()
def repo_trae(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), ApiKeyRepository(db), db
    db.close()

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

class UpstreamError(Exception):
    def __init__(self, kind: ErrKind) -> None:
        super().__init__(f"upstream {kind}")
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind

def build_executor(repo_tuple, script, **kwargs):
    credentials, _keys, _db = repo_tuple
    provider = FakeProvider(script)
    executor = Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                     scheduler=Scheduler(**kwargs), default_model="glm-5.2"))
    return executor, provider, credentials

def add_credential(credentials, **kw):
    return credentials.add(provider="trae", credential_data={"accessToken": "a"}, **kw)

GOOD_trae = [Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
        Event(kind=EventKind.FINISH, finish_reason="stop")]

async def test_stream_records_success_with_usage_and_username(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    event = collector.events[-1]
    assert event["ok"] is True and event["username"] == "alice"
    assert event["provider"] == "codebuddy" and event["model"] == "glm-5.2"
    assert event["input_tokens"] == 11 and event["output_tokens"] == 22
    assert event["reasoning_tokens"] == 3
    assert event["latency_ms"] is not None
    assert event["ttfb_ms"] is not None

async def test_stream_without_usage_frame_records_null_tokens(repo):
    """上游没给 usage 时统计字段为 None，不能崩也不能写 0。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(
        repo, [[Event(kind=EventKind.CONTENT, content="hi"),
                Event(kind=EventKind.FINISH, finish_reason="stop")]], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True})):
        pass
    assert collector.events[-1]["input_tokens"] is None

async def test_stream_prefers_credential_expiring_soon(repo):
    """到期积分指标贯通到真实选号：低健康度但快过期积分多的号先被选中。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    burn_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100,
                                            cycle_end=now + 48 * 3600, probed_at=now))
    credentials.save_quota(burn_id, Quota(remaining=10, total=100, cycle_end=now + 600,
                                           expiry_ladder=[(now + 600, 100.0),
                                                          (now + 700, 50.0)],
                                           probed_at=now))
    cands = {c.credential_id: c for c in credentials.candidates()}
    assert cands[burn_id].expiry_ladder == [(now + 600, 100.0), (now + 700, 50.0)]
    assert cands[steady_id].expiry_ladder is None
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == burn_id

async def test_stream_expiry_window_zero_keeps_health_order(repo):
    """窗口关闭后退回健康度排序（到期指标全员 0 分）。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    burn_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100, probed_at=now))
    credentials.save_quota(burn_id, Quota(remaining=10, total=100,
                                           expiry_ladder=[(now + 600, 100.0)],
                                           probed_at=now))
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector, expiry_window=0)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == steady_id

async def test_stream_secondary_expiry_breaks_tie(repo):
    """两级窗口贯通到真实选号：36h 内都没到期积分时，7 天内会过期者优先。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    week_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "w"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100, probed_at=now))
    credentials.save_quota(week_id, Quota(remaining=50, total=100,
                                          expiry_ladder=[(now + 3 * 86400, 140.0)],
                                          probed_at=now))
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == week_id

async def test_stream_records_failure_when_credentials_exhausted(repo):
    credentials, _db = repo
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="bob"):
        pass
    event = collector.events[-1]
    assert event["ok"] is False and event["error_type"] == "no_healthy_credential"
    assert event["username"] == "bob"

async def test_complete_records_success_and_failure(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}), username="alice")
    assert collector.events[-1]["ok"] is True
    assert collector.events[-1]["input_tokens"] == 11
    # 非流式也记首字延迟（上游首个事件时刻）
    assert collector.events[-1]["ttfb_ms"] is not None

    credentials.save_error(credentials.candidates()[0].credential_id, _disabled_outcome())
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "no_healthy_credential"

async def test_stats_failure_never_breaks_chat(repo):
    """统计后端不可用时聊天必须照常返回。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    collector.boom = True
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"

def test_executor_without_stats_collector_is_noop(repo):
    credentials, _db = repo
    executor = _exec_with_stats(repo, [GOOD_EVENTS], None)
    assert executor._deps.stats is None
    executor._deps.record(username="x", provider="y", model="z", ok=True)  # 不崩

async def test_upstream_http_failure_records_error_type(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Dead(Exception):
        def __init__(self) -> None:
            super().__init__("session dead")

        def kind(self):
            return ErrKind.DEAD

    executor = _exec_with_stats(repo, [[Dead()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    # 会话失效 → 硬禁用，轮换耗尽后归类为无可用凭证
    assert collector.events[-1]["error_type"] == "no_healthy_credential"
    assert credentials.candidates()[0].disabled is True

def test_error_type_mapping_covers_all_kinds():
    from src.engine.executor import _error_type_for
    from src.provider.base import ErrKind

    assert _error_type_for(ErrKind.PLAN) == "rate_limit"
    assert _error_type_for(ErrKind.MODEL) == "rate_limit"
    assert _error_type_for(ErrKind.CONCURRENCY) == "rate_limit"
    assert _error_type_for(ErrKind.DEAD) == "credential_unavailable"
    assert _error_type_for(ErrKind.SOFT) == "upstream_error"
    assert _error_type_for(ErrKind.OTHER) == "upstream_error"

def test_usage_field_tolerates_missing_usage():
    from src.engine.executor import _usage_field
    from src.provider.base import Usage

    assert _usage_field(None, "input_tokens") is None
    assert _usage_field(Usage(5, 6, 7), "output_tokens") == 6
    assert _usage_field(Usage(), "credit") is None

async def test_plan_error_records_rate_limit_error_type(repo):
    """权益耗尽 → 统计记为 rate_limit（受控错误类型白名单内）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Plan(Exception):
        def __init__(self) -> None:
            super().__init__("plan exhausted")

        def kind(self):
            return ErrKind.PLAN

    executor = _exec_with_stats(repo, [[Plan()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "no_healthy_credential"
    assert credentials.candidates()[0].cooling_until is not None

async def test_stream_plan_error_records_failure(repo):
    """流式路径的权益耗尽同样要落统计。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Plan(Exception):
        def __init__(self) -> None:
            super().__init__("plan exhausted")

        def kind(self):
            return ErrKind.PLAN

    executor = _exec_with_stats(repo, [[Plan()]], collector, max_rotate=1)
    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="carol"):
        pass
    assert collector.events[-1]["ok"] is False
    assert collector.events[-1]["username"] == "carol"

async def test_executor_complete_success(repo_trae, tmp_path):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"

async def test_executor_stream_success_frames(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks[-1] == SSE_DONE
    assert b'"hi"' in chunks[0]                      # 首帧即带 role + content

async def test_executor_records_estimated_credit_flag(repo_trae):
    """执行引擎把 provider 的 credit_estimated 标记带进统计明细。"""
    from src.stats.collector import StatsCollector

    credentials, _keys, db = repo_trae
    add_credential(credentials)
    collector = StatsCollector(db)
    usage = Usage(1000, 200, 0, credit=0.05, credit_estimated=True)
    script = [[Event(kind=EventKind.CONTENT, content="hi"),
               Event(kind=EventKind.USAGE, usage=usage),
               Event(kind=EventKind.FINISH, finish_reason="stop")]]
    executor = Executor(ExecutorDeps(
        providers={"trae": FakeProvider(script)}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks[-1] == SSE_DONE
    row = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events").fetchone()
    assert row["credit"] == 0.05 and row["credit_estimated"] == 1

async def test_executor_complete_records_credit_and_flag(repo_trae):
    """非流式路径同样把 USAGE 事件的 credit / 推算标记写进统计（否则整条漏掉）。"""
    from src.stats.collector import StatsCollector

    credentials, _keys, db = repo_trae
    add_credential(credentials)
    collector = StatsCollector(db)
    usage = Usage(1000, 200, 0, credit=0.07, credit_estimated=True)
    script = [[Event(kind=EventKind.CONTENT, content="ok"),
               Event(kind=EventKind.USAGE, usage=usage),
               Event(kind=EventKind.FINISH, finish_reason="stop")]]
    executor = Executor(ExecutorDeps(
        providers={"trae": FakeProvider(script)}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    row = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events").fetchone()
    assert row["credit"] == 0.07 and row["credit_estimated"] == 1

async def test_executor_rotates_on_http_error(repo_trae):
    add_credential(repo_trae[0], nickname="first")
    add_credential(repo_trae[0], nickname="second")
    script = [[UpstreamError(ErrKind.SOFT)], GOOD_trae]
    executor, provider, _credentials = build_executor(repo_trae, script)
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2

async def test_executor_cools_plan_error_then_fails_when_exhausted(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, credentials = build_executor(repo_trae, [[UpstreamError(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert credentials.candidates()[0].cooling_until is not None

async def test_executor_stream_reports_no_credential(repo_trae):
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"no_healthy_credential" in chunks[0]

async def test_executor_stream_inline_error_triggers_rotation(repo_trae):
    add_credential(repo_trae[0])
    add_credential(repo_trae[0])
    script = [[Event(kind=EventKind.ERROR, error_code=1005, error_message="quota")], GOOD_trae]
    executor, provider, _credentials = build_executor(repo_trae, script)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert provider.calls == 2 and chunks[-1] == SSE_DONE

async def test_executor_unknown_error_propagates(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [[RuntimeError("boom")]])
    with pytest.raises(RuntimeError):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

async def test_executor_dead_error_disables_credential(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, credentials = build_executor(repo_trae, [[UpstreamError(ErrKind.DEAD)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert credentials.candidates()[0].disabled is True

async def test_executor_reports_no_credential_as_service_unavailable(repo_trae):
    """provider 已注册但无凭证 → NoHealthyCredential（503），不是 400。"""
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

async def test_executor_rejects_model_without_provider(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    with pytest.raises(NoProviderForModel):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "model": "x@codebuddy"}))

@pytest.mark.asyncio
async def test_executor_auto_continues_on_length_same_credential(dual_repo):
    """finish_reason=length → 同凭证续写；executor 只看到一条更长的事件流。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="part1"),
                 Event(kind=EventKind.USAGE, usage=Usage(input_tokens=7,
                                                         output_tokens=3)),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    completed = [Event(kind=EventKind.CONTENT, content="part2"),
                 Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2,
                                                         output_tokens=4)),
                 Event(kind=EventKind.FINISH, finish_reason="stop")]
    provider = DualProvider("codebuddy", [truncated, completed])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", max_auto_continues=10))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "part1part2"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert provider.calls == 2                       # 同凭证两次请求
    # 累计用量：input 7+2、output 3+4
    assert result["usage"]["prompt_tokens"] == 9
    assert result["usage"]["completion_tokens"] == 7
    # 只用一个凭证，未发生轮换
    assert len(credentials.candidates(["codebuddy"])) == 1
    db.close()

@pytest.mark.asyncio
async def test_executor_auto_continue_disabled(dual_repo):
    """max_auto_continues=0（默认）：length 原样透传，不续写。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="part1"),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    provider = DualProvider("codebuddy", [truncated])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "part1"
    assert result["choices"][0]["finish_reason"] == "length"
    assert provider.calls == 1
    db.close()

@pytest.mark.asyncio
async def test_executor_stream_auto_continue_emits_single_finish(dual_repo):
    """流式续写：客户端只看到一条流、一个 finish_reason 与一条 [DONE]。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="a"),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    completed = [Event(kind=EventKind.CONTENT, content="b"),
                 Event(kind=EventKind.FINISH, finish_reason="stop")]
    provider = DualProvider("codebuddy", [truncated, completed])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", max_auto_continues=10))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert joined.count("data: [DONE]") == 1
    assert '"finish_reason": "stop"' in joined
    assert '"finish_reason": "length"' not in joined
    assert provider.calls == 2
    db.close()

async def test_dual_provider_same_model_routes_and_fails_over(dual_repo):
    """同一扁平模型名下，一个上游失败 → 自动落到另一个（Q21=C 的核心价值）。

    用 pin 固定首个上游，避免依赖同分时的字母序挑选。
    """
    credentials, db = dual_repo
    trae_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    credentials.set_pinned(trae_id)          # 固定先走 trae
    executor, trae, codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.SOFT)]], [GOOD])
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and codebuddy.calls == 1
    assert credentials.candidates(["trae"])[0].cooling_until is not None
    assert credentials.candidates(["codebuddy"])[0].cooling_until is None
    db.close()

async def test_dual_provider_forced_suffix_targets_single_upstream(dual_repo):
    """model@codebuddy 强制只走 codebuddy，不落到 trae。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, trae, codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    await executor.complete(_request("glm-5.2@codebuddy"))
    assert codebuddy.calls == 1 and trae.calls == 0
    db.close()

async def test_dual_provider_error_classification_is_consistent(dual_repo):
    """两个 provider 的错误分类都落到同一冷却语义（Q25=A 验证点）。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, _trae, _codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.PLAN)]], [[Boom(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    for candidate in credentials.candidates():
        assert candidate.cooling_until is not None, candidate
    db.close()

async def test_dual_provider_no_credential_when_both_registered(dual_repo):
    credentials, db = dual_repo
    executor, _trae, _codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    db.close()
