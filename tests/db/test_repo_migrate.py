"""凭证仓储：schema 迁移声明与老库升级（列/表补齐）。"""

from __future__ import annotations

import json
import sqlite3

from src.db.conn import Database
from src.db.migrate import apply_schema


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
