"""凭证仓储：模型级冷却、token 到期、积分流水、schema 升级。"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, CreditEventRepository
from src.engine.scheduler import ErrorOutcome
from src.main import build_app
from src.provider.base import ErrKind, Quota
from src.provider.codebuddy.client import CodeBuddyCredential
from src.stats.collector import StatsCollector
from src.tasks.retention import RetentionTask
from tests.helpers.constants import SECRET


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

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

def jwt_expiry_of(token: str) -> int:
    from src.provider.token_expiry import jwt_expiry

    return jwt_expiry(token)

def test_migration_declarations_match_schema_sql():
    """防漂移：迁移声明的列必须在 schema.sql 里存在，删表清单不得重建。

    `_MIGRATION_COLUMNS` 里的列名写错、或 schema.sql 删掉了对应的新库列定义，
    都会让两边静默分叉（新库/老库结构不一致），这里在测试期就抦住。
    """
    from src.db.migrate import _MIGRATION_COLUMNS, _MIGRATION_DROPS, _read_schema

    schema = _read_schema()
    for table, column_def in _MIGRATION_COLUMNS:
        column = column_def.split()[0]
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
        # 取该表定义段落，断言列在其中
        segment = schema.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)[1]
        segment = segment.split(");", 1)[0]
        assert column in segment, f"{table}.{column} 不在 schema.sql"
    for table in _MIGRATION_DROPS:
        # 删掉的表不得在 schema.sql 里重建，否则每次启动都会建了又删
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in schema, table

def test_migrate_adds_cached_tokens_to_legacy_db(tmp_path):
    """老库升级：usage_events 无 cached_tokens 列时幂等补齐，且可写入。"""
    import sqlite3

    from src.db.migrate import SCHEMA_VERSION, apply_schema

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE usage_events (
            id TEXT PRIMARY KEY, ts INTEGER NOT NULL, username TEXT NOT NULL,
            provider TEXT NOT NULL, credential_id TEXT, model TEXT NOT NULL,
            ok INTEGER NOT NULL, error_type TEXT, input_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER, credit REAL,
            latency_ms INTEGER, ttfb_ms INTEGER)
    """)
    # 旧版 usage_hourly：无 ttfb_sum 列（图表指标升级前的结构）
    conn.execute("""
        CREATE TABLE usage_hourly (
            hour_utc INTEGER NOT NULL, username TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0, ok_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            credit_sum REAL, credit_known INTEGER NOT NULL DEFAULT 0,
            latency_sum INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_utc, username, provider, model))
    """)
    # 该表内已有一行历史汇总：补列后必须保留，新列取默认 0
    conn.execute("INSERT INTO usage_hourly (hour_utc, username, provider, model, requests) "
                 "VALUES (1, 'u', 'trae', 'm', 5)")
    # 旧版 credentials：无 quota_expiry_ladder 列（到期排序指标升级前的结构）
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.commit()
    # 老库遗留的废弃表（schema.sql 已删定义，但老库里还在）
    conn.execute("CREATE TABLE checkins (provider TEXT, account_key TEXT)")
    conn.execute("CREATE TABLE model_cache (provider TEXT, model_id TEXT)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    events_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(usage_events)")}
    assert "cached_tokens" in events_columns
    # 积分推算标记（TRAE 无上游积分）：老库补 0=历史行全按非推算处理
    assert "credit_estimated" in events_columns
    # 成本估算两列（OpenRouter 刊例价 × 当时汇率）：老库补列后历史行为 NULL
    assert {"cost_usd", "cost_cny"} <= events_columns
    hourly_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(usage_hourly)")}
    assert "ttfb_sum" in hourly_columns
    assert "credit_estimated_known" in hourly_columns
    # 成本聚合三列：老库补列后历史小时 cost_known=0（聚合值不可信）
    assert {"cost_usd_sum", "cost_cny_sum", "cost_known"} <= hourly_columns
    # 总览改读小时汇总后新增的三列（老库历史行补 0，数值无法回填）
    assert {"reasoning_tokens", "cached_tokens", "cached_known"} <= hourly_columns
    legacy_hourly = db.connect().execute(
        "SELECT requests, reasoning_tokens, cached_known FROM usage_hourly "
        "WHERE hour_utc = 1").fetchone()
    assert tuple(legacy_hourly) == (5, 0, 0)      # 历史汇总保留，新列取默认
    legacy_est = db.connect().execute(
        "SELECT credit_estimated_known FROM usage_hourly WHERE hour_utc = 1").fetchone()
    assert legacy_est[0] == 0                     # 老库历史行按非推算
    legacy_cost = db.connect().execute(
        "SELECT cost_known, cost_usd_sum FROM usage_hourly WHERE hour_utc = 1").fetchone()
    assert tuple(legacy_cost) == (0, None)        # 老库历史行无成本（cost_known=0）
    cred_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert "quota_expiry_ladder" in cred_columns
    # 额度包明细（展示用）也是本次新增列
    assert "quota_packages" in cred_columns
    # 废弃表被 _MIGRATION_DROPS 清理（schema.sql 删定义对老库无效）
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "checkins" not in tables and "model_cache" not in tables
    # 新增表（(凭证, 模型) 冷却）也由启动时的 schema.sql 全量补建
    assert "credential_model_cooldowns" in tables
    # 运行时配置覆盖表（B3.2）同样是「只加表、不加列」，老库升级后必须存在且可写
    assert "runtime_settings" in tables
    # 账号体系（B5）：users / audit_events 也是「只加表」，老库升级后必须建好
    assert "users" in tables and "audit_events" in tables
    # 补列后可写入、可读出
    db.connect().execute(
        "INSERT INTO credentials (id, provider, data_enc, quota_expiry_ladder, quota_packages,"
        " created_at) VALUES ('cred_1', 'codebuddy', 'x', '[[123, 100.0]]',"
        " '[{\"name\": \"福利积分\"}]', 1)")
    db.connect().commit()
    assert db.connect().execute(
        "SELECT quota_expiry_ladder FROM credentials WHERE id = 'cred_1'").fetchone()[0] == \
        "[[123, 100.0]]"
    assert json.loads(db.connect().execute(
        "SELECT quota_packages FROM credentials WHERE id = 'cred_1'").fetchone()[0]) == \
        [{"name": "福利积分"}]
    # 运行时配置覆盖表：补建后可写入读出（老库升级后管理台立刻可用）
    db.connect().execute(
        "INSERT INTO runtime_settings (key, value, updated_at) "
        "VALUES ('quota_probe_minutes', '15', 1)")
    db.connect().commit()
    stored = db.connect().execute(
        "SELECT value FROM runtime_settings WHERE key = 'quota_probe_minutes'").fetchone()[0]
    assert stored == "15"
    # schema 版本推进到位；补列后新聚合可写
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.connect().execute(
        "INSERT INTO usage_hourly (hour_utc, username, provider, model, requests,"
        " ok_count, ttfb_sum) VALUES (2, 'u', 'trae', 'm', 1, 1, 120)")
    db.connect().commit()
    apply_schema(db.connect())                          # 二次执行不抛错
    db.close()

def test_migrate_reraises_non_duplicate_errors():
    """迁移中非 duplicate column 的 OperationalError 必须重新抛出。"""
    import pytest as _pytest

    class _FakeConn:
        OperationalError = sqlite3.OperationalError

        def executescript(self, _script):
            pass

        def execute(self, _sql):
            raise sqlite3.OperationalError("no such table")

        def commit(self):
            pass

    with _pytest.raises(sqlite3.OperationalError):
        apply_schema(_FakeConn())

def test_repo_persists_model_cooldowns_separately_from_account(repo):
    """模型级冷却只写独立表：账号级 cooling_until 必须保持为空。"""
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(
        err_count=0, model_cooldowns={"glm-5.2": ModelCooldown(
            cooling_until=9999, hits=1, reason="model")}))

    candidate = credentials.candidates()[0]
    assert candidate.cooling_until is None            # 账号级未被污染
    assert not candidate.is_selectable(1000, "glm-5.2")
    assert candidate.is_selectable(1000, "kimi-k2")   # 其他模型仍可用
    assert credentials.model_cooldowns_for(cid, now=1000) == {"glm-5.2": 9999}

def test_repo_model_cooldown_upsert_escalates_hits(repo):
    """同 (凭证, 模型) 反复命中时原地升级 hits，不产生多行。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=100, hits=1, reason="model")}))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=200, hits=2, reason="model")}))
    rows = db.connect().execute(
        "SELECT cooling_until, hits, reason FROM credential_model_cooldowns").fetchall()
    assert [(r["cooling_until"], r["hits"], r["reason"]) for r in rows] == [(200, 2, "model")]

def test_repo_candidates_carry_reason_so_backoff_escalates(repo):
    """candidates() 必须把 reason 一并发出来，否则 blocked 退避永远停在 6h。

    note_error 用 reason 判断「是否同一原因」；漏读会让每次都被当成换了原因
    而把 hits 重置为 1，6h→12h→24h 的升级形同虚设。
    """
    from src.engine.scheduler import Scheduler

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    scheduler = Scheduler()
    now = 1_000_000

    def note(**kw):
        candidate = credentials.candidates()[0]
        outcome = scheduler.note_error(candidate, ErrKind.BLOCKED, now,
                                       model="glm-5.2", **kw)
        credentials.save_error(cid, outcome)
        return outcome.model_cooldowns["glm-5.2"]

    first = note()
    assert (first.hits, first.reason) == (1, "blocked")
    assert credentials.candidates()[0].model_cooldowns["glm-5.2"].reason == "blocked"
    second = note()
    assert second.hits == 2 and second.cooling_until == now + 12 * 3600
    third = note()
    assert third.hits == 3 and third.cooling_until == now + 24 * 3600

def test_repo_account_cooldown_clears_model_entries(repo):
    """账号级冷却必须清空模型级条目：否则「切模型」能绕过账号级限流。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    credentials.save_error(cid, ErrorOutcome(cooling_until=8888, err_count=0))
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 0

def test_repo_save_success_clears_blocked_only(repo):
    """成功清除 negative cache，但不清 6004 模型级限流（对齐上游重置）。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "blocked-model": ModelCooldown(cooling_until=9999, hits=1, reason="blocked"),
        "limited-model": ModelCooldown(cooling_until=9999, hits=1, reason="model")}))
    credentials.save_success(cid, model="blocked-model")
    remaining = {r["model"] for r in db.connect().execute(
        "SELECT model FROM credential_model_cooldowns").fetchall()}
    assert remaining == {"limited-model"}
    # 不传 model 时不做任何模型级清理
    credentials.save_success(cid)
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 1

def test_repo_revive_and_delete_clear_model_cooldowns(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    assert credentials.revive(cid) is True
    count = db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"]
    assert count == 0
    # 再次写入后删除凭证：残留行必须一并清掉（重建同 id 不应继承旧冷却）
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    assert credentials.delete(cid) is True
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 0

def test_repo_purges_only_expired_model_cooldowns(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1),
        "fresh": ModelCooldown(cooling_until=5000, hits=1)}))
    assert credentials.purge_expired_model_cooldowns(now=1000) == 1
    names = {r["model"] for r in db.connect().execute(
        "SELECT model FROM credential_model_cooldowns").fetchall()}
    assert names == {"fresh"}

def test_repo_model_cooldowns_for_skips_expired(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1),
        "fresh": ModelCooldown(cooling_until=5000, hits=1)}))
    assert credentials.model_cooldowns_for(cid, now=1000) == {"fresh": 5000}

def test_retention_task_purges_expired_model_cooldowns(repo):
    """留存任务顺带回收过期冷却行；未注入凭证仓储时该计数为 0。"""
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=int(time.time()) - 10, hits=1)}))
    collector = StatsCollector(_db)
    report = RetentionTask(collector, credentials=credentials).run_once()
    assert report["expired_coolings"] == 1
    # 不传 credentials（旧调用方）时不报错，计数为 0
    assert RetentionTask(collector).run_once()["expired_coolings"] == 0

def test_retention_task_prunes_audit_events(repo):
    """留存任务按保留期回收审计流水；未注入时计数为 0（README「审计保留 90 天」）。"""
    from src.db.repo import AuditRepository

    _credentials, db = repo
    audit = AuditRepository(db)
    # 两条都远在过去；retention_days=0 → cutoff=当前时刻，两条都 < cutoff 即被删
    audit.record(actor="root", action="login.success", now=100)
    audit.record(actor="root", action="login.failure", now=200)
    collector = StatsCollector(db)
    report = RetentionTask(collector, retention_days=0, audit=audit).run_once()
    assert report["purged_audit"] == 2
    assert audit.query() == []
    # 不传 audit（旧调用方）时不报错，计数为 0
    assert RetentionTask(collector).run_once()["purged_audit"] == 0

def test_jwt_expiry_decodes_exp():
    """JWT payload 的 exp 是可用的到期来源；非 JWT / 坏结构一律 0。"""
    from src.provider.token_expiry import jwt_expiry

    assert jwt_expiry(_jwt(1794389367)) == 1794389367
    # 毫秒时间戳归一
    assert jwt_expiry(_jwt(1794389367000)) == 1794389367
    # 非 JWT、段数不足、payload 非 base64、payload 非对象、exp 非法
    assert jwt_expiry("not-a-jwt") == 0
    assert jwt_expiry("a.b") == 0
    assert jwt_expiry("h.!!!.s") == 0
    # payload 是合法 base64 但不是 JSON 对象（数组）→ 0
    assert jwt_expiry(f"h.{_b64([1, 2])}.s") == 0
    assert jwt_expiry(_jwt(0)) == 0
    assert jwt_expiry(_jwt(True)) == 0
    assert jwt_expiry(_jwt("soon")) == 0      # type: ignore[arg-type]
    assert jwt_expiry(12345) == 0             # type: ignore[arg-type]
    # 缺 payload 段（"a..c"）也要挡住，不能把空串当 JSON 解
    assert jwt_expiry("a..c") == 0

def test_jwt_times_decodes_iat_and_exp():
    """`(iat, exp)` 一起取：进度条要 iat 当满量程，缺它就只能退化（见 display）。"""
    from src.provider.token_expiry import jwt_times

    token = _jwt(1794389367, iat=1793189367)
    assert jwt_times(token) == (1793189367, 1794389367)
    # iat 缺失 / 非法 → 0（而 exp 照常给），两者互不拖累
    assert jwt_times(_jwt(1794389367)) == (0, 1794389367)
    assert jwt_times(_jwt(1794389367, iat=0)) == (0, 1794389367)
    assert jwt_times(_jwt(1794389367, iat=True)) == (0, 1794389367)
    assert jwt_times(_jwt(1794389367, iat="x")) == (0, 1794389367)  # type: ignore[arg-type]
    # 毫秒时间戳归一（两个 claim 各自归一）
    assert jwt_times(_jwt(1794389367000, iat=1793189367000)) == (1793189367, 1794389367)
    # 非 JWT / 坏结构 → (0, 0)
    assert jwt_times("not-a-jwt") == (0, 0)
    assert jwt_times("a..c") == (0, 0)
    assert jwt_times(f"h.{_b64([1, 2])}.s") == (0, 0)
    assert jwt_times(12345) == (0, 0)         # type: ignore[arg-type]

def test_credential_expiry_prefers_explicit_then_jwt():
    """显式 expires_at 优先；缺失才回落 JWT exp；两者都没有 → 0（未知）。"""
    from src.provider.token_expiry import credential_expiry

    token = _jwt(1794389367)
    assert credential_expiry({"expires_at": 100}) == 100
    assert credential_expiry({"expiresAt": 200}) == 200
    # `expiration`：华为云系（CodeArts STS 临时凭证）的字段名。漏掉它时该渠道
    # `token_expires_at` 恒为 0 → 管理台无到期预览、到期预警对它完全失效。
    assert credential_expiry({"expiration": 300}) == 300
    assert credential_expiry({"expiration": 1794389367000}) == 1794389367   # 毫秒
    assert credential_expiry({"expires_at": 100, "expiration": 300}) == 100  # 前者优先
    assert credential_expiry({"expiration": "soon"}) == 0                   # 非数字
    # 显式值为 0 / 非数字 → 视作缺失，回落 JWT
    assert credential_expiry({"expires_at": 0, "bearer_token": token}) == 1794389367
    assert credential_expiry({"expires_at": True, "accessToken": token}) == 1794389367
    assert credential_expiry({"access_token": token}) == 1794389367
    assert credential_expiry({"token": token}) == 1794389367
    # 无任何 token / 键值类型不对 → 0，绝不猜本地 TTL
    assert credential_expiry({}) == 0
    assert credential_expiry({"bearer_token": None}) == 0
    assert credential_expiry({"bearer_token": "plain-token"}) == 0
    assert credential_expiry([]) == 0         # type: ignore[arg-type]

def test_credential_token_times_borrows_iat_from_co_issued_refresh_token():
    """access token 不是 JWT 时，借**同时签发**的 refresh_token JWT 的 `iat`。

    CodeArts 的 access token 是 STS AK/SK（取不到 iat），但签发它的那次响应同时
    轮转出一张 refresh_token JWT，`iat` 就是临时凭证的签发时刻（实测
    `iat + 7200 == expiration`）。只借 iat：它自己的 `exp` 是「登录 +30 天」，
    拿来做 access token 的到期会把 2h 的临时凭证画成 30 天。
    """
    from src.provider.token_expiry import credential_token_times

    issued = 1_700_000_000
    refresh_token = _jwt(issued + 30 * 86400, iat=issued)
    # CodeArts 形态：到期来自 `expiration`，签发借 refresh_token 的 iat
    assert credential_token_times({"expiration": issued + 7200,
                                   "refresh_token": refresh_token}) == (issued, issued + 7200)
    # access token 本身是 JWT 时优先用它自己的 iat（借 iat 只在兜底时发生）
    assert credential_token_times({
        "expiration": issued + 7200, "refresh_token": refresh_token,
        "bearer_token": _jwt(issued + 7200, iat=issued - 5),
    }) == (issued - 5, issued + 7200)
    # refresh_token 不是 JWT / 没有 iat → 签发时间仍旧 0（绝不拿回填时刻冒充）
    assert credential_token_times({"expiration": 1_700_007_200,
                                   "refresh_token": "plain"}) == (0, 1_700_007_200)
    assert credential_token_times({"expiration": 1_700_007_200, "refresh_token": ""}) == (
        0, 1_700_007_200)

def test_credential_token_times_pairs_issued_with_expiry():
    """签发时间只来自 JWT `iat`：上游不单独回传，拿不到就是 0（绝不拿回填时刻冒充）。"""
    from src.provider.token_expiry import credential_token_times

    token = _jwt(1794389367, iat=1793189367)
    # 显式 expires_at 优先；iat 仍从 JWT 取（两者不同源，各取所长）
    assert credential_token_times(
        {"expires_at": 100, "bearer_token": token}) == (1793189367, 100)
    assert credential_token_times(
        {"expiresAt": 200, "accessToken": token}) == (1793189367, 200)
    # 无显式值 → 到期回落 JWT exp
    assert credential_token_times({"bearer_token": token}) == (1793189367, 1794389367)
    # 无 iat 的 JWT：到期照给，签发时间未知 → 0
    assert credential_token_times({"bearer_token": _jwt(42)}) == (0, 42)
    # 非 JWT 的明文 token / 没有 token 字段 → 全 0（不猜）
    assert credential_token_times({"bearer_token": "plain"}) == (0, 0)
    assert credential_token_times({"expires_at": 7}) == (0, 7)
    assert credential_token_times({"bearer_token": None}) == (0, 0)
    assert credential_token_times([]) == (0, 0)   # type: ignore[arg-type]
    # 高优先键是明文、低优先键才是 JWT → 继续找，别丢签发时间
    assert credential_token_times(
        {"bearer_token": "plain", "token": token}) == (1793189367, 1794389367)
    # 高优先键是坏 JWT 也一样跳过
    assert credential_token_times(
        {"bearer_token": _jwt(0), "access_token": token}) == (1793189367, 1794389367)

def test_codebuddy_token_expiry_falls_back_to_jwt():
    """实测 CB 的 token 响应不带到期字段：必须回落到 JWT exp，否则预刷新永不触发。"""
    token = _jwt(int(time.time()) + 50 * 86400)
    credential = CodeBuddyCredential.from_dict(
        {"bearer_token": token, "refresh_token": "r", "auth_source": "oauth"})
    assert credential.expires_at == 0                       # JSON 里确实没有显式值
    assert credential.token_expires_at() == jwt_expiry_of(token)
    # 预刷新：进入 24h 窗口才 True，早于此 False（修复前恒 False）
    assert credential.needs_refresh(24 * 3600) is False
    assert credential.needs_refresh(50 * 86400) is True
    # 显式 expires_at 仍然优先于 JWT
    explicit = CodeBuddyCredential.from_dict(
        {"bearer_token": token, "refresh_token": "r", "expires_at": 12345})
    assert explicit.token_expires_at() == 12345

def test_codebuddy_token_expiry_unknown_is_not_expired():
    """拿不到到期时间 → 过期未知，不刷新（none 分支），绝不猜本地 TTL。"""
    credential = CodeBuddyCredential.from_dict(
        {"bearer_token": "plain", "refresh_token": "r", "auth_source": "oauth"})
    assert credential.token_expires_at() == 0
    assert credential.needs_refresh(0) is False
    # bearer-only 手动凭证永不刷新（AGENTS.md），即便 token 里带 exp
    manual = CodeBuddyCredential.from_dict({"bearer_token": _jwt(1), "refresh_token": "r"})
    assert manual.needs_refresh(999_999_999) is False

def test_trae_token_expiry_summary_and_jwt_fallback():
    """TRAE 侧同一口径：`expires_at` 只存显式值，缺失时由派生方法回落 JWT exp。"""
    from src.provider.trae.credential import TraeCredential

    token = _jwt(int(time.time()) + 10 * 86400)
    plain = TraeCredential.from_dict({"accessToken": token, "expiresAt": 0})
    # 显式字段缺失/为 0 → 仍存 0（JWT 派生值不进字段，避免写回后变「权威」）
    assert plain.expires_at == 0
    assert plain.token_expires_at() > time.time() + 9 * 86400
    explicit = TraeCredential.from_dict({"accessToken": token, "expiresAt": 12345})
    assert explicit.expires_at == 12345
    # 刷新写回时 to_dict() 只带显式值，JWT 派生值不会残留在 JSON 里
    assert plain.to_dict()["expiresAt"] == 0
    # 完全拿不到到期时间：TRAE 保守刷新（既有契约）
    unknown = TraeCredential.from_dict({"accessToken": "plain-token"})
    assert unknown.expires_at == 0
    assert unknown.token_expires_at() == 0
    assert unknown.needs_refresh(3600) is True

def test_repo_persists_token_times_on_add_and_save(tmp_path):
    """add / save_credential_data 都写回 token_expires_at 与 token_issued_at。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    token = _jwt(now + 30 * 86400, iat=now)
    cid = credentials.add(provider="codebuddy",
                          credential_data={"bearer_token": token}, now=now)
    stored = db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (cid,)).fetchone()
    assert tuple(stored) == (now + 30 * 86400, now)
    # 刷新写回：两个时间一并推进（旧值被替换，不残留成「权威」到期）
    rotated = _jwt(now + 60 * 86400 + 100, iat=now + 100)
    credentials.save_credential_data(cid, {"bearer_token": rotated})
    assert tuple(db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (cid,)).fetchone()) == (now + 60 * 86400 + 100, now + 100)
    # 上游没给任何到期信息 → 写 0（未知），不得猜本地 TTL
    unknown = credentials.add(provider="codebuddy",
                              credential_data={"bearer_token": "plain"}, now=now)
    assert tuple(db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (unknown,)).fetchone()) == (0, 0)
    db.close()

def test_list_all_derives_token_times_from_legacy_rows(tmp_path):
    """老库升级后两列为 NULL：列表读到时按需从密文派生。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    token = _jwt(now + 5 * 86400, iat=now)
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": token},
                          now=now)
    # 模拟升级前写入的行：两列都清空（迁移只加列，不回填）
    db.connect().execute(
        "UPDATE credentials SET token_expires_at = NULL, token_issued_at = NULL WHERE id = ?",
        (cid,))
    db.connect().commit()
    row = credentials.list_all(now=now)[0]
    assert row["token_expires_at"] == now + 5 * 86400
    assert row["token_issued_at"] == now
    # data_enc 绝不进响应
    assert "data_enc" not in row
    db.close()

def test_list_all_token_expiry_unknown_stays_zero(tmp_path):
    """到期时间未知写 0：只下发绝对值，剩余时间与预警由前端同一时钟现算。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    known = credentials.add(provider="codebuddy",
                            credential_data={"bearer_token": _jwt(now + 600)}, now=now)
    unknown = credentials.add(provider="codebuddy",
                              credential_data={"bearer_token": "plain"}, now=now)
    rows = {row["id"]: row for row in credentials.list_all(now=now)}
    assert rows[known]["token_expires_at"] == now + 600
    # 未知 = 0，展示层据此隐藏（绝不当成已过期）
    assert rows[unknown]["token_expires_at"] == 0
    assert rows[unknown]["token_issued_at"] == 0
    # 列表只给绝对值，不替前端算剩余时间（服务端算好的不会随 tick 更新）
    assert "token_remaining_seconds" not in rows[known]
    assert "token_expiring" not in rows[known]
    db.close()

def test_token_times_from_blob_tolerates_bad_ciphertext(tmp_path):
    """密文损坏 / 空值不得让列表整体崩掉（返回 (0, 0) = 未知）。"""
    from src.db.crypto import CredentialCipher
    from src.db.repo import _token_times_from_blob

    cipher = CredentialCipher(SECRET)
    assert _token_times_from_blob(None, cipher) == (0, 0)
    assert _token_times_from_blob(b"", cipher) == (0, 0)
    assert _token_times_from_blob(b"not-a-fernet-token", cipher) == (0, 0)
    # 解密成功但内容不是 JSON 对象
    assert _token_times_from_blob(cipher.encrypt(b"[1, 2]"), cipher) == (0, 0)
    assert _token_times_from_blob(cipher.encrypt(b"not json"), cipher) == (0, 0)
    # 正常载荷：两列一起给出
    payload = json.dumps({"bearer_token": _jwt(200, iat=100)}).encode("utf-8")
    assert _token_times_from_blob(cipher.encrypt(payload), cipher) == (100, 200)

def test_legacy_db_upgrade_adds_token_columns(tmp_path):
    """老库升级：credentials 无 token 两列时幂等补列。"""
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc BLOB NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.commit()
    conn.close()
    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert {"token_expires_at", "token_issued_at"} <= columns
    # 补列后可写可读，ver 推进
    db.connect().execute(
        "INSERT INTO credentials (id, provider, data_enc, token_expires_at,"
        " token_issued_at, created_at) VALUES ('cred_x', 'codebuddy', 'x', 123, 100, 1)")
    db.connect().commit()
    assert tuple(db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = 'cred_x'"
    ).fetchone()) == (123, 100)
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()

def test_save_quota_records_net_credit_change(tmp_path):
    """余额变化才记流水，且记的是净变化，不是动作归因。

    上游签到/成长接口不打日志；这里断言 source 只有 observed/sync 两种，
    绝不出现 checkpoint/growth 这类上游从未告知的归因。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)

    # 首次探测：只建立基线（sync），不算积分
    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now))
    first = events.recent(cid)
    assert len(first) == 1
    assert first[0]["source"] == "sync" and first[0]["delta"] is None
    assert first[0]["after"] == 100 and first[0]["before"] is None

    # 余额没变 → 不记（否则每轮探测落一行 0，表被噪声淹没）
    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now + 60))
    assert len(events.recent(cid)) == 1
    # 浮点噪声不算变化：上游 JSON 的浮点运算会给出 100.00000000000001 这种值
    credentials.save_quota(cid, Quota(remaining=100 + 1e-13, total=200, probed_at=now + 90))
    assert len(events.recent(cid)) == 1

    # 余额增加（签到/成长/任何来源都可能）→ 记净变化 + 区间起点
    credentials.save_quota(cid, Quota(remaining=115, total=200, probed_at=now + 120))
    latest = events.recent(cid)[0]
    assert latest["delta"] == 15              # 落库前已按 epsilon 收敛，不残留 14.999999
    assert latest["before"] == pytest.approx(100) and latest["after"] == 115
    # 区间起点是「上次探测」，不是「上次有变动」：中间那两次没记行但都观测过，
    # 覆盖区间必须从最近一次观测算起，否则会把一段无人观测的时间也算进去
    assert latest["window_start"] == now + 90
    assert latest["source"] == "observed"

    # 余额减少（对话消耗）→ delta 为负，同样如实记
    credentials.save_quota(cid, Quota(remaining=110, total=200, probed_at=now + 180))
    assert events.recent(cid)[0]["delta"] == -5
    db.close()

def test_save_quota_records_unquantifiable_change(tmp_path):
    """余额变未知（探测失败后）仍记行：这是异常，但绝不量化成 0。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)

    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now))
    credentials.mark_probe_failed(cid, now=now + 60)
    credentials.save_quota(cid, Quota(remaining=None, total=None, probed_at=now + 120))
    latest = events.recent(cid)[0]
    assert latest["delta"] is None            # 不猜 0
    assert latest["before"] == 100 and latest["after"] is None
    assert latest["source"] == "observed"
    db.close()

def test_save_quota_skips_event_for_unknown_to_unknown(tmp_path):
    """两端都未知 = 没学到任何东西，不写空行。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=None, total=None, probed_at=now))
    assert events.recent(cid) == []
    db.close()

def test_save_quota_event_skipped_when_credential_gone(tmp_path):
    """凭证被并发删除：不补记悬挂行（查询返回 None 的分支）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    credentials.save_quota("cred_missing", Quota(remaining=1, total=2, probed_at=1))
    assert events.recent("cred_missing") == []
    db.close()

def test_credit_events_recent_is_bounded_and_ordered(tmp_path):
    """倒序返回，limit 收敛到 [1, 200]（防止一次拉爆前端）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=100)
    for step in range(5):
        credentials.save_quota(cid, Quota(remaining=100 - step, total=200, probed_at=100 + step))
    rows = events.recent(cid)
    assert [row["ts"] for row in rows] == [104, 103, 102, 101, 100]  # 最新在前
    assert len(events.recent(cid, limit=2)) == 2
    assert len(events.recent(cid, limit=0)) == 1        # 下限收敛
    assert len(events.recent(cid, limit=9999)) == 5     # 上限不报错
    # 别的凭证的记录不会串进来
    other = credentials.add(provider="trae", credential_data={"accessToken": "t"}, now=100)
    assert events.recent(other) == []
    db.close()

def test_credit_events_prune_drops_old_rows(tmp_path):
    """保留期清理：只删过期行，保留期内不动。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=1, total=2, probed_at=now - 100 * 86400))
    credentials.save_quota(cid, Quota(remaining=2, total=2, probed_at=now))
    assert events.prune(keep_days=90, now=now) == 1
    assert [row["after"] for row in events.recent(cid)] == [2]
    db.close()

def test_credited_events_endpoint_requires_admin_and_known_credential(tmp_path):
    """端到端：非管理员 403；未知凭证 400；已知凭证返回流水。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        now = int(time.time())
        repo = app.state.credentials
        cid = repo.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
        repo.save_quota(cid, Quota(remaining=50, total=100, probed_at=now))
        repo.save_quota(cid, Quota(remaining=60, total=100, probed_at=now + 60))

        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        body = client.get(f"/api/credentials/{cid}/credit-events").json()
        assert [event["delta"] for event in body["events"]] == [10, None]
        assert body["events"][1]["source"] == "sync"
        # limit 透传
        assert len(client.get(
            f"/api/credentials/{cid}/credit-events?limit=1").json()["events"]) == 1
        # 未知凭证 → 400（不是空列表，避免拼错 id 时看起来「没有记录」）
        assert client.get("/api/credentials/cred_nope/credit-events").status_code == 400
        # 非管理员（conftest 里存在的 alice）→ 403
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        assert client.get(f"/api/credentials/{cid}/credit-events").status_code == 403

def test_retention_task_prunes_credit_events(tmp_path):
    """保留任务顺带回收积分流水；未传 credit_events 时该键为 0（老调用方兼容）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=1, total=2, probed_at=now - 100 * 86400))
    credentials.save_quota(cid, Quota(remaining=2, total=2, probed_at=now))

    class FakeCollector:
        def rollup_hourly(self) -> int:
            return 3

        def purge_expired(self, days: int) -> int:
            return 7

    task = RetentionTask(FakeCollector(), retention_days=90,
                         credentials=credentials, credit_events=events)
    report = task.run_once()
    assert report["rolled_up"] == 3 and report["purged"] == 7
    assert report["purged_credit_events"] == 1
    # 老调用方（不传 credit_events）仍然可用
    bare = RetentionTask(FakeCollector(), retention_days=90, credentials=credentials)
    assert bare.run_once()["purged_credit_events"] == 0
    db.close()

def test_legacy_db_upgrade_adds_credit_events_table(tmp_path):
    """老库升级：apply_schema 建出 credit_events（CREATE IF NOT EXISTS 生效）。"""
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("CREATE TABLE credentials (id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
                 " data_enc BLOB NOT NULL, created_at INTEGER NOT NULL)")
    conn.commit()
    conn.close()
    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credit_events)")}
    assert columns == {"id", "credential_id", "ts", "window_start",
                       "before", "after", "delta", "source"}
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()

def test_legacy_db_upgrade_adds_api_key_policy_columns(tmp_path):
    """老库升级：api_keys 无 provider_binding / allowed_ips 时幂等补列。

    两列都是 `NOT NULL DEFAULT ''`：补列后老 Key 立刻恢复「不限定渠道、
    不限制来源 IP」的原行为，不需要回填。
    """
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY, username TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            key_digest TEXT NOT NULL UNIQUE, preview TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_used_at INTEGER)
    """)
    # 老库里已有一条 Key：补列后必须保留且取默认空串
    conn.execute("INSERT INTO api_keys (id, username, key_digest, preview, created_at) "
                 "VALUES ('key_old', 'root', 'digest', 'sk-…old', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(api_keys)")}
    assert {"provider_binding", "allowed_ips"} <= columns
    legacy = db.connect().execute(
        "SELECT provider_binding, allowed_ips FROM api_keys WHERE id = 'key_old'").fetchone()
    assert tuple(legacy) == ("", "")
    # 补列后可写可读；ver 推进
    db.connect().execute(
        "UPDATE api_keys SET provider_binding = 'trae', allowed_ips = '10.0.0.0/8' "
        "WHERE id = 'key_old'")
    db.connect().commit()
    assert tuple(db.connect().execute(
        "SELECT provider_binding, allowed_ips FROM api_keys WHERE id = 'key_old'").fetchone()
    ) == ("trae", "10.0.0.0/8")
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()

def test_credential_edit_lock_is_reused_per_id(repo):
    """编辑锁按凭证 id 分键复用：同 id 返回同一把锁，不同 id 各自独立（M12）。"""
    credentials, db = repo
    first = credentials.edit_lock("cred_a")
    assert credentials.edit_lock("cred_a") is first
    assert credentials.edit_lock("cred_b") is not first
    db.close()
