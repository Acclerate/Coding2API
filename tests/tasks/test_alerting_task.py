"""运维告警：AlertTask 静默去重、webhook 投递与脱敏。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from src.tasks.alerting import SEVERITY_CRITICAL, SEVERITY_WARNING, Alert
from src.tasks.status import TASK_BY_KEY
from tests.tasks._alerting_support import StubCredentials as _StubCredentials
from tests.tasks._alerting_support import make_alert_task as _task


async def test_alert_task_disabled_returns_none(tmp_path):
    task, alerts, db = _task(tmp_path, enabled=lambda: False)
    assert await task.run_once() is None
    assert alerts.recent() == []
    db.close()

async def test_alert_task_fires_and_records_without_webhook(tmp_path):
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        now=lambda: 1000)
    report = await task.run_once()
    assert report == {"evaluated": 1, "fired": 1, "suppressed": 0, "delivered": 0}
    row = alerts.recent()[0]
    assert row["rule"] == "pool_empty" and row["delivered"] == 0
    assert row["delivery_error"] is None
    assert json.loads(row["detail"])["ready"] == 0
    db.close()

async def test_alert_task_silences_repeats_then_refires(tmp_path):
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        silence_minutes=lambda: 30, now=lambda: 1000)
    assert (await task.run_once())["fired"] == 1
    # 静默窗内：命中但被抑制，不再落库
    second = await task.run_once()
    assert second == {"evaluated": 1, "fired": 0, "suppressed": 1, "delivered": 0}
    assert len(alerts.recent()) == 1
    # 窗口过后仍命中 → 再报一次
    task._now = lambda: 1000 + 30 * 60
    assert (await task.run_once())["fired"] == 1
    assert len(alerts.recent()) == 2
    db.close()

async def test_alert_task_silence_zero_always_fires(tmp_path):
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        silence_minutes=lambda: 0, now=lambda: 1000)
    await task.run_once()
    await task.run_once()
    assert len(alerts.recent()) == 2
    db.close()

async def test_alert_task_delivers_to_webhook(tmp_path):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 1
    assert alerts.recent()[0]["delivered"] == 1
    assert seen[0]["rule"] == "pool_empty" and seen[0]["severity"] == SEVERITY_CRITICAL
    await client.aclose()
    db.close()

async def test_alert_task_webhook_failure_does_not_raise(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 5)
    report = await task.run_once()
    assert report["fired"] == 1 and report["delivered"] == 0
    row = alerts.recent()[0]
    assert row["delivered"] == 0 and row["delivery_error"] is not None
    await client.aclose()
    db.close()

async def test_alert_task_multiple_webhooks_one_fails(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500 if request.url.path.endswith("/bad") else 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/ok, https://hook.example/bad",
        client=client, now=lambda: 5)
    report = await task.run_once()
    assert len(calls) == 2
    assert report["delivered"] == 0          # 有一个失败即整体不算投递成功
    assert alerts.recent()[0]["delivery_error"] is not None
    await client.aclose()
    db.close()

async def test_alert_task_two_webhooks_both_fail_keeps_first_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a, https://hook.example/b",
        client=client, now=lambda: 5)
    await task.run_once()
    # 两个都失败时只保留首个错误，不互相覆盖
    assert alerts.recent()[0]["delivery_error"] is not None
    await client.aclose()
    db.close()

async def test_alert_task_redacts_webhook_secret_from_log_and_db(tmp_path, caplog):
    """M5：webhook URL 的 path/query 常即密钥，日志与落库都不得带上整条 URL。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    secret_url = "https://hooks.example/T000/B000/super-secret-token"
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: secret_url, client=client, now=lambda: 5)
    with caplog.at_level("WARNING"):
        await task.run_once()
    # 日志只留 host，不带密钥 path
    assert "super-secret-token" not in caplog.text
    assert "hooks.example" in caplog.text
    # 落库的 delivery_error 同样不含密钥（httpx 异常文本会内嵌整条 URL）
    error = alerts.recent()[0]["delivery_error"]
    assert "super-secret-token" not in error
    await client.aclose()
    db.close()

def test_redact_webhook_url_falls_back_for_opaque_value():
    """非 URL 的 webhook 值（如误配成裸主机名）也一律脱敏为占位串。"""
    from src.tasks.alerting import _redact_webhook_url

    assert _redact_webhook_url("not-a-url") == "<webhook>"
    assert _redact_webhook_url("https://hook.example/x") == "https://hook.example"

async def test_alert_task_default_client_path(tmp_path, monkeypatch):
    """未注入 client 时走自建 httpx.AsyncClient（生产路径）。"""
    captured: list[dict] = []

    class _FakeResponse:
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            captured.append({"url": url, "json": json})
            return _FakeResponse()

    monkeypatch.setattr("src.tasks.alerting.httpx.AsyncClient", _FakeClient)
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 1
    assert captured[1]["url"] == "https://hook.example/a"
    db.close()

async def test_alert_task_expiring_uses_remaining(tmp_path):
    expiring = [{"id": "c1", "provider": "trae", "nickname": "x",
                 "token_expires_at": 1300}]
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(expiring=expiring),
        token_expiry_hours=lambda: 1, now=lambda: 1000)
    report = await task.run_once()
    assert report["fired"] == 1
    assert json.loads(alerts.recent()[0]["detail"])["remaining"] == 300
    db.close()

async def test_alert_task_expiring_off_skips_lookup(tmp_path):
    class _Exploding(_StubCredentials):
        def expiring_tokens(self, **_kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("窗口为 0 时不应查库")

    task, _alerts, db = _task(tmp_path, credentials=_Exploding(),
                              token_expiry_hours=lambda: 0, now=lambda: 1000)
    assert (await task.run_once())["evaluated"] == 0
    db.close()

async def test_alert_task_webhook_redacts_pii_nickname(tmp_path):
    """nickname 常被填成邮箱/手机号：站内留原文，外发 webhook 必须打码。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    expiring = [{"id": "c1", "provider": "trae", "nickname": "user@example.com",
                 "token_expires_at": 1300}]
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(expiring=expiring),
        token_expiry_hours=lambda: 1,
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 1000)
    report = await task.run_once()
    assert report["delivered"] == 1
    # 站内记录保留原文，便于定位
    assert "user@example.com" in json.loads(alerts.recent()[0]["detail"])["nickname"]
    # 外发 payload 的 message / detail 都不得含 PII
    assert "user@example.com" not in json.dumps(seen[0], ensure_ascii=False)
    assert seen[0]["detail"]["nickname"] == "***"
    await client.aclose()
    db.close()


async def test_alert_task_empty_nickname_not_masked_in_payload(tmp_path):
    """昵称为空时不产生 `***` 噪声（保持空串）。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    expiring = [{"id": "c1", "provider": "trae", "nickname": "",
                 "token_expires_at": 1300}]
    task, _alerts, db = _task(
        tmp_path, credentials=_StubCredentials(expiring=expiring),
        token_expiry_hours=lambda: 1,
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 1000)
    await task.run_once()
    assert seen[0]["detail"]["nickname"] == ""
    await client.aclose()
    db.close()


async def test_alert_task_webhook_total_budget_exceeded(tmp_path, monkeypatch):
    """多地址总投递超预算：本轮判失败，不把后台循环拖死。"""
    monkeypatch.setattr("src.tasks.alerting._WEBHOOK_TOTAL_BUDGET", 0.01)

    class _SlowClient:
        async def post(self, url, json=None):
            await asyncio.sleep(1)
            return httpx.Response(200)

    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=_SlowClient(),
        now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 0
    assert alerts.recent()[0]["delivery_error"] is not None
    db.close()


def test_public_detail_masks_only_present_pii():
    from src.tasks.alerting import _public_detail

    assert _public_detail({"nickname": "a@b.c", "provider": "trae"}) == {
        "nickname": "***", "provider": "trae"}
    assert _public_detail({"nickname": "", "provider": "trae"}) == {
        "nickname": "", "provider": "trae"}


def test_alert_public_message_masks_pii_fragments():
    from src.tasks.alerting import Alert

    alert = Alert(rule="token_expiring", severity=SEVERITY_WARNING, scope="c1",
                  message="凭证「a@b.c」即将到期", pii=("a@b.c",))
    assert alert.public_message() == "凭证「***」即将到期"
    # 空片段不参与替换（避免把整条文案打散）
    assert Alert(rule="r", severity=SEVERITY_WARNING, scope="s",
                 message="x", pii=("",)).public_message() == "x"


def test_alert_task_spec_registered():
    assert TASK_BY_KEY["alert"].name == "运维告警"

def test_alert_humanize_boundaries():
    """_humanize 三段分支都要走到（小时 / 分钟 / 秒）。"""
    from src.tasks.alerting import _humanize

    assert _humanize(3600) == "1.0 小时"
    assert _humanize(3599) == "59 分钟"
    assert _humanize(59) == "59 秒"

def test_alert_dataclass_dedup_key():
    alert = Alert(rule="pool_empty", severity="critical", scope="pool", message="x")
    assert alert.dedup_key == "pool_empty:pool"

def test_alert_severity_warning_constant():
    assert SEVERITY_WARNING == "warning"
    assert SEVERITY_CRITICAL == "critical"
    assert isinstance(time.time(), float)
