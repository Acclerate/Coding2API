"""运维告警：令牌到期窗口、错误率统计、仓储与 /api/alerts 端点、设置热更。"""

from __future__ import annotations

import base64
import json

import pytest

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import AlertRepository
from src.stats.collector import StatsCollector
from tests.helpers.constants import SECRET


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"

# ------------------------------------------------- CredentialRepository.expiring_tokens


def test_expiring_tokens_window_and_exclusions(repo):
    credentials, db = repo
    now = 1_000_000
    # 落在窗口内
    soon = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "expires_at": now + 3600}, nickname="甲", now=now)
    # 已过期（在 now 之前）→ 不算「即将到期」
    credentials.add(provider="trae", credential_data={
        "bearer_token": "t", "expires_at": now - 10}, now=now)
    # 窗口外（超出 within）
    credentials.add(provider="trae", credential_data={
        "bearer_token": "t", "expires_at": now + 100_000}, now=now)
    # 到期未知（0）→ 不当成已过期
    credentials.add(provider="trae", credential_data={"bearer_token": "t"}, now=now)
    # 硬禁用凭证排除
    dead = credentials.add(provider="trae", credential_data={
        "bearer_token": "t", "expires_at": now + 60}, now=now)
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (dead,))
    db.connect().commit()

    rows = credentials.expiring_tokens(within_seconds=7200, now=now)
    assert [r["id"] for r in rows] == [soon]
    assert rows[0]["nickname"] == "甲" and rows[0]["provider"] == "codebuddy"

def test_expiring_tokens_derives_from_blob_when_column_null(repo):
    """token_expires_at 列为 NULL（老库未回填）时从密文派生。"""
    credentials, db = repo
    now = 1_000_000
    cid = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": _jwt({"exp": now + 1800})}, now=now)
    db.connect().execute(
        "UPDATE credentials SET token_expires_at = NULL WHERE id = ?", (cid,))
    db.connect().commit()
    rows = credentials.expiring_tokens(within_seconds=3600, now=now)
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["token_expires_at"] == now + 1800

def test_expiring_tokens_off_returns_empty(repo):
    credentials, _db = repo
    assert credentials.expiring_tokens(within_seconds=0, now=1) == []

def test_expiring_tokens_skips_tokens_shorter_lived_than_window(repo):
    """寿命本就短于窗口的 token 不报：命中是常态，规则没有信息量。

    CodeArts 的 STS 临时凭证只有 2h 寿命，默认窗口 24h——不管续期成功与否它都
    永远「即将到期」，每个静默窗报一次只会把真告警淹掉。它真出问题时预刷新任务
    会失败并标记需重新登录（进而触发池空告警）。
    """
    credentials, _db = repo
    now = 1_000_000
    # 2h 寿命（expiration 与 refresh_token JWT 的 iat 相差 7200）
    credentials.add(provider="codearts", credential_data={
        "expiration": now + 3600,
        "refresh_token": _jwt({"iat": now + 3600 - 7200, "exp": now + 30 * 86400})},
        now=now)
    # 30 天寿命的同形态凭证：窗口 24h 相对它很短，必须照报
    long_lived = credentials.add(provider="codearts", credential_data={
        "expiration": now + 3600,
        "refresh_token": _jwt({"iat": now + 3600 - 30 * 86400, "exp": now + 30 * 86400})},
        now=now)
    rows = credentials.expiring_tokens(within_seconds=86400, now=now)
    assert [r["id"] for r in rows] == [long_lived]

# --------------------------------------------------------- StatsCollector


def test_window_error_rate_counts_requests_and_failures(repo):
    _credentials, db = repo
    stats = StatsCollector(db)
    stats.record(username="u", provider="trae", model="m", ok=True, now=1000)
    stats.record(username="u", provider="trae", model="m", ok=False, now=1100)
    stats.record(username="u", provider="trae", model="m", ok=False, now=1200)
    requests, failed = stats.window_error_rate(since=1000)
    assert (requests, failed) == (3, 2)
    # 窗口外不计
    assert stats.window_error_rate(since=1150) == (1, 1)

# --------------------------------------------------------- AlertRepository


def test_alert_repository_record_recent_last_ts_prune(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="池空", detail="{}", now=100)
    repo.record(rule="task_failed", severity="warning", scope="growth",
                message="挂了", now=200)
    recent = repo.recent()
    assert [r["ts"] for r in recent] == [200, 100]
    assert repo.last_ts("pool_empty", "pool") == 100
    assert repo.last_ts("pool_empty", "other") is None
    # limit 收敛到 [1, 200]
    assert len(repo.recent(limit=0)) == 1
    assert repo.prune(keep_days=1, now=100 + 2 * 86400) == 2
    assert repo.recent() == []
    db.close()

def test_alert_repository_records_delivery_failure(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    repo.record(rule="error_rate", severity="warning", scope="pool",
                message="错误率高", delivered=False, delivery_error="boom", now=1)
    row = repo.recent()[0]
    assert row["delivered"] == 0 and row["delivery_error"] == "boom"
    db.close()

def test_alerts_requires_admin(admin_client):
    _app, client = admin_client
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get("/api/alerts").status_code == 403

def test_alerts_returns_recent(admin_client):
    app, client = admin_client
    app.state.services.alerts.record(
        rule="task_failed", severity="warning", scope="growth",
        message="挂了", now=123)
    body = client.get("/api/alerts").json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["rule"] == "task_failed"

def test_settings_snapshot_carries_alert_owner(admin_client):
    _app, client = admin_client
    by_key = {item["key"]: item for item in client.get("/api/settings").json()["settings"]}
    assert by_key["alert_enabled"]["task"] == "alert"
    assert by_key["alert_webhook_url"]["task"] == "alert"
    assert by_key["alert_error_rate_threshold"]["task"] == "alert"
    assert by_key["alert_enabled"]["group"] is None

def test_alert_settings_hot_update(tmp_path):
    """告警阈值可热更：写入后立即生效，恢复默认回落 env。"""
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import load_runtime_settings

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET)
    runtime = load_runtime_settings(base, RuntimeSettingsRepository(db))
    assert runtime.alert_task_failures == 3
    runtime.set_many({"alert_task_failures": 5, "alert_webhook_url": "https://x"})
    assert runtime.alert_task_failures == 5
    assert runtime.alert_webhook_url == "https://x"
    runtime.set_many({"alert_task_failures": None, "alert_webhook_url": None})
    assert runtime.alert_task_failures == 3
    assert runtime.alert_webhook_url == ""
    db.close()

def test_alert_webhook_rejects_non_http_scheme_and_too_many(tmp_path):
    """M6：webhook 只接受 http/https，且条目数有上限。"""

    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import InvalidSetting, load_runtime_settings

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET)
    runtime = load_runtime_settings(base, RuntimeSettingsRepository(db))
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x"):
        with pytest.raises(InvalidSetting, match="只支持 http/https"):
            runtime.set_many({"alert_webhook_url": bad})
    too_many = ",".join(f"https://h{i}" for i in range(20))
    with pytest.raises(InvalidSetting, match="最多"):
        runtime.set_many({"alert_webhook_url": too_many})
    # SSRF：内网/回环/链路本地（云元数据）地址一律拒
    for blocked in ("http://127.0.0.1:9000/x", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.5/hook", "http://[::1]:80/hook",
                    "http://localhost/hook", "http://224.0.0.1/hook",
                    "http://0.0.0.0/hook"):
        with pytest.raises(InvalidSetting, match="内网/回环/保留"):
            runtime.set_many({"alert_webhook_url": blocked})
    # 无主机名
    with pytest.raises(InvalidSetting, match="缺少主机名"):
        runtime.set_many({"alert_webhook_url": "http:///nohost"})
    # 逗号分隔的合法多地址通过
    runtime.set_many({"alert_webhook_url": "https://a, http://b"})
    assert runtime.alert_webhook_url == "https://a, http://b"
    db.close()


def test_env_webhook_url_is_validated_at_load(tmp_path):
    """env 提供的 ALERT_WEBHOOK_URL 也要过校验（不能只靠运行时失败兜底）。"""
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import InvalidSetting, load_runtime_settings

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET,
                    ALERT_WEBHOOK_URL="http://169.254.169.254/latest")
    with pytest.raises(InvalidSetting, match="内网/回环/保留"):
        load_runtime_settings(base, RuntimeSettingsRepository(db))
    db.close()
