"""CodeBuddy 成长 runner 抽奖/buddy box 与任务装配接线。"""

from __future__ import annotations

import httpx
import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, GrowthRepository
from src.provider.base import GrowthResult, GrowthStep, StepStatus
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.growth import (
    EP_BUDDY_OPEN,
    EP_BUDDY_QUOTA,
    EP_LOTTERY_CHANCES,
    EP_LOTTERY_DRAW,
    EP_REDEEM_SUMMARY,
    EP_STREAK,
    EP_TASKS,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_STATUS,
    CodeBuddyGrowth,
)
from src.provider.codebuddy.growth_runner import GrowthRunner
from src.tasks.growth import GrowthTask
from src.tasks.runner import TaskRunner, build_runner

SECRET = "test-secret-0123456789"

TRAVEL_IDLE = {"code": 0, "data": {
    "state": "idle", "location": {"id": 1, "name": "咖啡馆"},
    "daily_limit_reached": False}}

STREAK_BODY = {"code": 0, "data": {
    "streak": {"days": 0, "month_total_days": 3, "makeup_dates": []},
    "makeup_cards": {"balance": 0, "max": 4},
    "redemption_status": {"tier_7d_status": "locked", "tier_14d_status": "locked",
                          "tier_28d_status": "locked"}}}

REDEEM_LOCKED = {"code": 0, "data": {
    "starter_status": "locked", "advanced_status": "locked", "legendary_status": "locked",
    "starter_count": 0, "total_consumed": 0}}

def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)

def _scripted(handler) -> CodeBuddyGrowth:
    return CodeBuddyGrowth("https://e", client=_client(handler))

def _runner(handler, *, allow_irreversible: bool = True) -> GrowthRunner:
    return GrowthRunner(_scripted(handler), allow_irreversible=allow_irreversible)

async def _run(handler, *, allow_irreversible: bool = True):
    runner = _runner(handler, allow_irreversible=allow_irreversible)
    return await runner.run(CodeBuddyCredential(bearer_token="t"))

@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "growth.sqlite3")
    apply_schema(db.connect())
    cipher = CredentialCipher(SECRET)
    yield CredentialRepository(db, cipher), GrowthRepository(db)
    db.close()

class _StubGrowthProvider:
    """可控的成长中心 provider：记录调用、可注入结果或异常。"""

    id = "codebuddy"

    def __init__(self, *, result=None, error=None, scope="acct") -> None:
        self.calls = 0
        self._result = result or GrowthResult(
            ok=True, report="领旅行礼物：+10 积分", credit=10.0,
            steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "+10 积分")])
        self._error = error
        self._scope = scope
        self.allow_flags: list[bool] = []

    async def growth(self, _data, *, allow_irreversible: bool = True):
        self.calls += 1
        self.allow_flags.append(allow_irreversible)
        if self._error is not None:
            raise self._error
        return self._result

    def checkin_scope(self, data):
        # 与真实 provider 同款语义：身份未知（含空 scope）时返回空串，
        # 由任务回落到 credential_id（绝不共享）
        identity = data.get("account_uid", "")
        if not self._scope or not identity:
            return ""
        return f"{self._scope}|{identity}"

async def _noop(*_args, **_kwargs):
    return None

async def test_runner_lottery_no_chance_and_failure_paths():
    """抽奖次数为 0 → 完全不请求 draw；draw 失败分业务态与硬失败。"""
    called: list[str] = []

    async def no_chance(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(no_chance)
    assert EP_LOTTERY_DRAW not in called and EP_BUDDY_OPEN not in called
    assert result.ok is True

    async def draw_business_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(400, json={"code": 400,
                                             "msg": "insufficient lottery chance balance"})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(draw_business_error)
    assert result2.ok is True                        # 次数为 0 是常态，不是故障
    assert result2.gained is False

    async def draw_broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(500, content=b"boom")
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result3 = await _run(draw_broken)
    assert result3.ok is False

async def test_runner_lottery_prize_shapes_and_buddy_box_failure():
    """奖品名可能是对象/数字；Buddy 盲盒失败要记失败。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            # prize 是数字 + require_address：不能因类型报错而丢掉整次中奖
            return httpx.Response(200, json={"code": 0, "data": {
                "prize": 3, "require_address": True}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {
                "affordable": 9, "max_open_count": 3}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(400, json={"code": 400, "msg": "能量不足"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    draw = [step for step in result.steps if step.name == "开盲盒"
            and step.status == StepStatus.DONE]
    assert draw[0].detail == "3（实物奖，需到成长中心填写收件信息）"
    box = [step for step in result.steps if step.name == "Buddy 盲盒"]
    # 能量不足是业务规则（上限夹取后仍被上游拒）→ 记 IDLE 不算硬失败
    assert box[0].status == StepStatus.IDLE and result.ok is True
    assert "能量不足" in box[0].detail

    async def broken_box(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 1}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(broken_box)
    assert result2.ok is False
    assert any(step.name == "Buddy 盲盒" and step.status == StepStatus.FAILED
               for step in result2.steps)

async def test_runner_buddy_box_reports_default_name_and_gain_flags():
    """Buddy 名字缺失时回落「新 Buddy」；gained/failed 属性口径稳定。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 1}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(200, json={"code": 0, "data": {"buddies": [1, 2]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.gained is True and result.failed == []
    box = next(step for step in result.steps if step.name == "Buddy 盲盒")
    assert box.detail == "×1（新 Buddy）"        # bundles 是列表 → 不是 str → 回落

def test_build_runner_wires_growth_task(repo):
    """build_runner 传入 events 仓库时装配成长中心；不传则不装配（无落库目标）。"""
    credentials, events = repo
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="./data",
                        GROWTH_INTERVAL_MINUTES=30, GROWTH_IRREVERSIBLE_ACTIONS=False)
    runner = build_runner(credentials, {}, None, settings, growth_events=events)
    assert runner._growth is not None
    assert runner._growth._allow_irreversible is False
    assert runner._growth_interval == 1800
    assert build_runner(credentials, {}, None, settings)._growth is None

def test_build_runner_clamps_growth_interval(repo):
    """成长中心间隔下限 5 分钟：请求量比签到大一个量级，更密只会撞风控。"""
    credentials, events = repo
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="./data",
                        GROWTH_INTERVAL_MINUTES=0)
    runner = build_runner(credentials, {}, None, settings, growth_events=events)
    assert runner._growth_interval == 300

async def test_runner_sync_growth_and_loop_registration(repo):
    """成长中心循环被注册进 asyncio 任务列表，_sync_growth 委托给任务对象。"""
    credentials, events = repo
    provider = _StubGrowthProvider()
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = GrowthTask(credentials, {"codebuddy": provider}, events)
    runner = TaskRunner(quota_probe=type("Q", (), {"run_once": staticmethod(_noop)})(),
                        checkin=type("C", (), {"due": staticmethod(lambda: False)})(),
                        growth=task, refresh=type("R", (), {"run_once": _noop})(),
                        retention=type("T", (), {"run_once": staticmethod(lambda: None)})())
    await runner._sync_growth()
    assert provider.calls == 1

async def test_runner_lottery_prize_missing_and_no_address_flag():
    """奖品字段全缺失 → 显示「未知」；没有实物标志就不追加提醒。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(200, json={"code": 0, "data": {"need_address": False}})
        return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})

    result = await _run(handler)
    draw = next(step for step in result.steps if step.name == "开盲盒")
    assert draw.detail == "未知"
    assert "实物奖" not in draw.detail
