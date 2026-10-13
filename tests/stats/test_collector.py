"""统计采集、小时汇总与保留期回收。"""

from __future__ import annotations

import time

import pytest

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.stats.collector import StatsCollector
from src.stats.query import StatsQuery
from src.tasks.retention import RetentionTask
from tests.helpers.constants import SECRET


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

@pytest.fixture()
def stats(repo):
    _credentials, db = repo
    yield StatsCollector(db), StatsQuery(db)

def test_stats_records_and_redacts(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=20, latency_ms=100, ttfb_ms=30)
    collector.record(username="alice", provider="codebuddy", model="glm-5.2", ok=False,
                     error_type="upstream_error", credit=0.5)
    overview = query.overview(username="alice")
    assert overview["requests"] == 2 and overview["ok_count"] == 1
    assert overview["success_rate"] == 0.5
    assert overview["input_tokens"] == 10
    assert overview["credit"] == 0.5
    assert overview["avg_latency_ms"] == 100
    assert overview["avg_ttfb_ms"] == 30
    by_provider = query.by_provider(username="alice")
    assert [row["provider"] for row in by_provider] == ["codebuddy", "trae"]

def test_stats_credit_is_null_when_never_known(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert query.overview(username="u")["credit"] is None
    assert query.overview(username="u")["credit_estimated"] is False

def test_stats_estimated_credit_flag_flows_through(stats):
    """推算积分：明细标 1、总览/按渠道标 ≈、events 带标记；无 credit 的推算标记被丢弃。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="deepseek-v4.1-flash", ok=True,
                     input_tokens=1000, output_tokens=200, credit=0.05,
                     credit_estimated=True)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, credit=1.5)
    # credit 缺失时推算标记无意义，落库应为 0
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     credit_estimated=True)

    overview = query.overview(username="u")
    assert overview["credit"] == 1.55
    assert overview["credit_estimated"] is True

    rows = {row["provider"]: row for row in query.by_provider(username="u")}
    assert rows["trae"]["credit_estimated"] is True
    assert rows["codebuddy"]["credit_estimated"] is False

    events = query.events(username="u")["events"]
    by_model = {e["model"]: e for e in events}
    assert by_model["deepseek-v4.1-flash"]["credit_estimated"] == 1
    assert by_model["glm-5.2"]["credit_estimated"] == 0
    # 只有真值 credit 的渠道不标推算
    collector.record(username="v", provider="codebuddy", model="m", ok=True, credit=2.0)
    assert query.overview(username="v")["credit_estimated"] is False
    assert query.by_provider(username="v")[0]["credit_estimated"] is False

def test_stats_rollup_preserves_estimated_known(stats):
    """retention rollup 重算小时汇总时保留 credit_estimated_known。"""
    collector, query = stats
    now = int(time.time())
    collector.record(username="u", provider="trae", model="m", ok=True, credit=0.05,
                     credit_estimated=True, now=now)
    collector.record(username="u", provider="trae", model="m", ok=True, credit=0.10,
                     credit_estimated=True, now=now)
    collector.record(username="u", provider="trae", model="m", ok=True, credit=0.20,
                     now=now)
    collector.rollup_hourly(since=now - 10)
    row = query._db.connect().execute(
        "SELECT credit_estimated_known, credit_known FROM usage_hourly").fetchone()
    assert row["credit_estimated_known"] == 2
    assert row["credit_known"] == 3

def test_stats_normalizes_model_and_error_type(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="x" * 100, ok=False,
                     error_type="not-a-controlled-value", credit=-5)
    rows = query.by_provider(username="u")
    assert rows[0]["requests"] == 1
    row = query._db.connect().execute(
        "SELECT model, error_type, credit FROM usage_events").fetchone()
    assert row["model"] == "unknown" and row["error_type"] is None and row["credit"] is None

@pytest.mark.parametrize("model", [None, "", "has space", "bad\ncontrol", 5])
def test_safe_model_rejects_invalid(stats, model):
    collector, query = stats
    collector.record(username="u", provider="trae", model=model, ok=True)
    assert query._db.connect().execute(
        "SELECT model FROM usage_events").fetchone()["model"] == "unknown"

def test_purge_keeps_partially_populated_hour_intact(stats):
    """边界小时不得被删一半：否则下一轮 rollup 把汇总行覆盖成缩小值。

    rollup 对整行是 REPLACE 语义，purge 只删已完全过期的小时（切点向上
    对齐到小时边界），保证「仍有明细的小时保有全部明细」。
    """
    collector, _query = stats
    now = int(time.time())
    hour = (now // 3600) * 3600
    # 同一小时内两条明细：早的已过期（200 天前同一小时），晚的还在
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, now=hour - 200 * 86400 + 1)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=10, now=hour + 1)
    RetentionTask(collector, retention_days=90).run_once()
    row = collector._db.connect().execute(
        "SELECT SUM(input_tokens) AS t, SUM(requests) AS r FROM usage_hourly").fetchone()
    assert (row["t"], row["r"]) == (110, 2)      # 两条都在，未被截断

def test_stats_retention_and_rollup(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=5,
                     latency_ms=10, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=5,
                     latency_ms=20, now=2000)
    task = RetentionTask(collector, retention_days=90)
    first = task.run_once()
    assert first["rolled_up"] >= 1
    rows = query._db.connect().execute(
        "SELECT requests, input_tokens FROM usage_hourly").fetchall()
    assert sum(r["requests"] for r in rows) == 2
    # 清理：把 now 推到 91 天之后
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    assert collector.purge_expired(90, now=1000 + 91 * 86400) == 1
    # 小时汇总永久保留
    assert query._db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_hourly").fetchone()["c"] >= 1

def test_stats_rollup_is_idempotent(stats):
    collector, _query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=10_000)
    collector.rollup_hourly()
    collector.rollup_hourly()
    total = collector._db.connect().execute(
        "SELECT SUM(requests) AS s FROM usage_hourly").fetchone()["s"]
    assert total == 1

def test_stats_record_handles_negative_and_bool_tokens(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=-1, output_tokens=True, reasoning_tokens=None,
                     latency_ms=-5, ttfb_ms=True, credit=-1.0)
    row = query._db.connect().execute(
        "SELECT input_tokens, output_tokens, latency_ms, credit FROM usage_events").fetchone()
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["latency_ms"] is None and row["credit"] is None

def test_stats_record_rejects_non_finite_credit(stats):
    """L1：NaN/Inf 不进聚合（污染不可回滚），落库为 NULL。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credit=float("inf"))
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credit=float("nan"))
    rows = query._db.connect().execute(
        "SELECT credit FROM usage_events").fetchall()
    assert [r["credit"] for r in rows] == [None, None]

def test_stats_record_defaults_username_when_blank(stats):
    collector, query = stats
    collector.record(username="", provider="trae", model="m", ok=True)
    assert query.by_provider()[0]["requests"] == 1
