"""统计查询（overview / by-* / timeline / model-timeline / events）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.main import build_app
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

@pytest.fixture()
def admin_client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client

def test_stats_overview_global_for_admin(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="m", ok=True)
    collector.record(username="bob", provider="trae", model="m", ok=True)
    assert query.overview()["requests"] == 2
    assert query.overview(username="alice")["requests"] == 1

def test_stats_by_model_aggregates(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=100, output_tokens=50, credit=1.0)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=False)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     input_tokens=200, output_tokens=100, credit=2.0)
    rows = query.by_model(username="u")
    assert [row["model"] for row in rows] == ["glm-5.2", "kimi-k3"]
    glm = rows[0]
    assert glm["requests"] == 2 and glm["ok_count"] == 1
    assert glm["input_tokens"] == 100 and glm["output_tokens"] == 50
    assert glm["credit"] == 1.0
    kimi = rows[1]
    assert kimi["requests"] == 1 and kimi["ok_count"] == 1
    assert kimi["input_tokens"] == 200 and kimi["output_tokens"] == 100
    assert kimi["credit"] == 2.0

def test_stats_by_model_empty(stats):
    _collector, query = stats
    assert query.by_model() == []
    assert query.by_model(username="nobody") == []

def test_stats_by_user_aggregates(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=100, output_tokens=50, credit=1.0)
    collector.record(username="alice", provider="codebuddy", model="kimi-k3", ok=False)
    collector.record(username="bob", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, credit=0.5)
    rows = {row["username"]: row for row in query.by_user()}
    assert set(rows) == {"alice", "bob"}
    alice = rows["alice"]
    assert alice["requests"] == 2 and alice["ok_count"] == 1
    assert alice["input_tokens"] == 100 and alice["output_tokens"] == 50
    assert alice["credit"] == 1.0
    assert rows["bob"]["requests"] == 1 and rows["bob"]["ok_count"] == 1
    # 用户维度同样可按人过滤（viewer 只能看自己）
    assert [row["username"] for row in query.by_user(username="bob")] == ["bob"]
    assert query.by_user(username="nobody") == []

def test_stats_by_credential_aggregates(stats):
    """凭证维度读明细表（小时汇总主键里没有 credential_id）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     credential_id="cred_a", input_tokens=100, output_tokens=50,
                     cached_tokens=30, credit=1.0)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     credential_id="cred_a", input_tokens=10, output_tokens=5,
                     cached_tokens=0, credit=0.5)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     credential_id="cred_b", input_tokens=20, output_tokens=10,
                     credit=2.0)
    # 无凭证的请求（预热失败等）不可归属，不进分组
    collector.record(username="u", provider="trae", model="glm-5.2", ok=False,
                     error_type="upstream_error")

    rows = {row["credential_id"]: row for row in query.by_credential()}
    assert set(rows) == {"cred_a", "cred_b"}
    cred_a = rows["cred_a"]
    assert cred_a["requests"] == 2 and cred_a["ok_count"] == 2
    assert cred_a["input_tokens"] == 110 and cred_a["output_tokens"] == 55
    assert cred_a["cached_tokens"] == 30
    assert cred_a["credit"] == 1.5
    assert rows["cred_b"]["requests"] == 1
    # 无缓存上报时 cached_tokens 回 None（不把 0 当成真值）
    assert rows["cred_b"]["cached_tokens"] is None
    # 按用户/时间过滤同样生效（_where 拼的子句与 hourly 维度共用）
    assert query.by_credential(username="nobody") == []

def test_stats_by_credential_reports_name_and_provider(repo):
    """凭证分组随行下发渠道（画 icon）与昵称（与明细同口径显示凭证名）。

    昵称取自共享池，只对 operator 返回（M7）；渠道优先取凭证当前所属，
    凭证已删除则回退明细里的渠道。
    """
    credentials, db = repo
    collector, query = StatsCollector(db), StatsQuery(db)
    keep_id = credentials.add(provider="trae", credential_data={"accessToken": "t"},
                              nickname="主号")
    ghost_id = credentials.add(provider="trae", credential_data={"accessToken": "g"},
                               nickname="将删")
    blank_id = credentials.add(provider="codearts", credential_data={}, nickname="")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=keep_id)
    # 已删除凭证的明细跨渠道（凭证改过 provider）→ 回退取字典序最大的那个
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="codearts", model="m", ok=True,
                     credential_id=blank_id)
    assert credentials.delete(ghost_id) is True

    rows = {row["credential_id"]: row for row in
            query.by_credential(include_credential_name=True)}
    assert rows[keep_id]["credential_name"] == "主号"
    assert rows[keep_id]["provider"] == "trae"
    assert rows[ghost_id]["credential_name"] is None        # 已删除 → 回退
    assert rows[ghost_id]["provider"] == "trae"             # 回退明细渠道
    assert rows[blank_id]["credential_name"] is None        # 空昵称 → 同回退
    assert rows[blank_id]["provider"] == "codearts"

    # M7：默认（viewer 口径）不下发共享池昵称，渠道照常给（前端 icon 依赖）
    default_rows = {row["credential_id"]: row for row in query.by_credential()}
    assert default_rows[keep_id]["credential_name"] is None
    assert default_rows[keep_id]["provider"] == "trae"

def test_stats_by_credential_endpoint_gates_credential_name(admin_client, tmp_path):
    """by-credential 端点：operator 拿昵称；viewer 不下发昵称但仍拿渠道。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="trae",
                                              credential_data={"accessToken": "t"},
                                              nickname="主号")
    app.state.stats_collector.record(username="root", provider="trae", model="m", ok=True,
                                     credential_id=credential_id)
    row = client.get("/api/stats/by-credential").json()["credentials"][0]
    assert row["credential_name"] == "主号" and row["provider"] == "trae"

    # 非 operator 身份：共享池昵称不下发（M7），渠道 icon 依赖的 provider 照常
    reader_settings = Settings(_env_file=None, APP_SECRET=SECRET,
                               DATA_DIR=str(tmp_path / "reader"))
    reader_app = build_app(reader_settings)
    reader_cred = reader_app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "b"}, nickname="他人号")
    reader_app.state.stats_collector.record(username="alice", provider="codebuddy",
                                            model="m", ok=True,
                                            credential_id=reader_cred)
    with TestClient(reader_app) as viewer:
        viewer.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        seen = viewer.get("/api/stats/by-credential").json()["credentials"][0]
    assert seen["credential_name"] is None and seen["provider"] == "codebuddy"

def test_stats_groups_report_cached_tokens_and_known(stats):
    """各维度的缓存命中：任一明细上报过才给值，否则 None。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, cached_tokens=40)
    for rows in (query.by_provider(), query.by_model(), query.by_user()):
        assert rows[0]["cached_tokens"] == 40
    collector.record(username="v", provider="trae", model="m", ok=True)
    assert query.by_provider(username="v")[0]["cached_tokens"] is None

def test_stats_overview_empty_and_filters(stats):
    _collector, query = stats
    empty = query.overview()
    assert empty["requests"] == 0 and empty["success_rate"] is None
    assert empty["avg_latency_ms"] is None
    assert query.by_provider() == []

def test_overview_matches_timeline_beyond_retention_window(stats):
    """超过 90 天的时段：总览与图表必须同值（同读小时汇总）。

    此前总览读 usage_events（90 天）、图表读 usage_hourly（永久），
    选「全部」时同屏两个数字矛盾：明细被清理后总览缩水、图表不变。
    """
    collector, query = stats
    now = int(time.time())
    old = now - 200 * 86400               # 已超出 90 天明细保留期
    for ts in (old, now):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         input_tokens=5, now=ts)
    RetentionTask(collector, retention_days=90).run_once()
    assert query.overview(username="u")["requests"] == 2          # 含已过期的老明细
    assert sum(p["trae"] for p in query.timeline(username="u")) == 2

def test_overview_reasoning_and_cached_aggregate_from_hourly(stats):
    """小时表带上 reasoning/cached 后，总览仍能给准确值（与明细同源）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, reasoning_tokens=7, cached_tokens=40)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=50, reasoning_tokens=3)     # 未上报 cached
    collected = query.overview(username="u")
    assert collected["reasoning_tokens"] == 10
    assert collected["cached_tokens"] == 40      # 只有上报过的那条计入
    assert collected["input_tokens"] == 150
    # 未上报过 cached 的时段 → None，不能当成 0
    collector.record(username="v", provider="trae", model="m", ok=True)
    assert query.overview(username="v")["cached_tokens"] is None

def test_overview_latency_averages_only_successful_requests(stats):
    """均值口径与图表一致：除以 ok_count，失败请求不拉偏「典型耗时」。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=100, ttfb_ms=30)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=300, ttfb_ms=50)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="upstream_error", latency_ms=900, ttfb_ms=900)
    overview = query.overview(username="u")
    assert overview["avg_latency_ms"] == 200    # (100+300)/2，失败那条不计入
    assert overview["avg_ttfb_ms"] == 40

def test_stats_overview_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.overview(username="u", since=5000)["requests"] == 1

def test_stats_hourly_rolls_via_retention(stats):
    """record 即时累加汇总（总览/图表不必等一轮 retention）；rollup 幂等不双计。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1_700_003_600)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="rate_limit", now=1_700_003_601)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     now=1_700_003_602, input_tokens=7, output_tokens=3,
                     credit=0.5, latency_ms=100)
    # 增量累加：最新小时立即可见（不需等 5 分钟一轮的 rollup）
    points = query.timeline(username="u", since=1_700_000_000)
    assert len(points) == 1 and points[0]["trae"] == 2 and points[0]["codebuddy"] == 1
    # 全量重算与该小时的增量值一致（幂等，不双计）
    RetentionTask(collector, retention_days=90).run_once()
    points = query.timeline(username="u", since=1_700_000_000)
    assert len(points) == 1 and points[0]["trae"] == 2 and points[0]["codebuddy"] == 1
    bucket = collector._db.connect().execute(
        "SELECT SUM(requests), SUM(ok_count), SUM(input_tokens), SUM(output_tokens), "
        "SUM(credit_known), SUM(latency_sum) FROM usage_hourly").fetchone()
    assert tuple(bucket) == (3, 2, 7, 3, 1, 100)

def test_stats_timeline_by_hour_and_provider(stats):
    collector, query = stats
    # 同一小时两条明细（同一 provider 累积到同一点，跨 provider 分列）
    collector.record(username="u", provider="codebuddy", model="m", ok=True, now=1_700_000_000)
    collector.record(username="u", provider="codebuddy", model="m", ok=True, now=1_700_000_100)
    collector.record(username="u", provider="trae", model="m", ok=True, now=1_700_000_200)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     now=1_700_000_000 + 3600)  # 下一小时
    collector.rollup_hourly()
    points = query.timeline(username="u")
    # 两小时、每个小时都含 codebuddy/trae 两路计数
    assert len(points) == 2
    first = points[0]
    assert first["codebuddy"] == 2 and first["trae"] == 1
    assert points[1]["trae"] == 1
    # since 过滤按小时边界（第二小时整点 = first hour 整点 + 3600）
    second_hour = first["hour"] + 3600
    filtered = query.timeline(username="u", since=second_hour)
    assert len(filtered) == 1 and filtered[0]["trae"] == 1

def test_stats_timeline_metric_dimensions(stats):
    """timeline 支持请求数/token/耗时/首字四维；均值按成功数归一；非法回退。"""
    collector, query = stats
    base = 1_700_000_000
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     input_tokens=10, output_tokens=5, latency_ms=100, ttfb_ms=40, now=base)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     input_tokens=20, output_tokens=5, latency_ms=300, ttfb_ms=60, now=base)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, output_tokens=50, latency_ms=200, ttfb_ms=80, now=base)
    collector.rollup_hourly()

    req = query.timeline(username="u")  # 默认请求次数
    first = req[0]
    assert first["codebuddy"] == 2 and first["trae"] == 1
    # token = 输入 + 输出（跨渠道各自的合计）
    tok = query.timeline(username="u", metric="tokens")[0]
    assert tok["codebuddy"] == 40 and tok["trae"] == 150
    # 耗时/首字 = 成功请求均值：(100+300)/2=200，trae 单条即自身
    lat = query.timeline(username="u", metric="latency")[0]
    assert lat["codebuddy"] == 200 and lat["trae"] == 200
    ttf = query.timeline(username="u", metric="ttfb")[0]
    assert ttf["codebuddy"] == 50 and ttf["trae"] == 80
    # 非法 metric 回退请求次数
    assert query.timeline(username="u", metric="bogus") == req

def test_stats_model_timeline_metric_and_ttfb_rollup(stats):
    """model_timeline 指标切换 + ttfb 聚合进小时表（老库补列后可用）。"""
    collector, query = stats
    base = 1_700_000_000
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, latency_ms=100, ttfb_ms=40, now=base)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=20, output_tokens=5, latency_ms=300, ttfb_ms=60, now=base)
    collector.rollup_hourly()

    tok = query.model_timeline(username="u", metric="tokens")
    assert tok["points"][0]["glm-5.2"] == 40
    ttf = query.model_timeline(username="u", metric="ttfb")
    assert ttf["points"][0]["glm-5.2"] == 50
    # Top N 排序口径不随 metric 变（仍按请求量）
    assert query.model_timeline(username="u", metric="latency")["models"] == ["glm-5.2"]
    # ttfb 确实聚合进了小时表
    conn = collector._db.connect()
    row = conn.execute("SELECT ttfb_sum, latency_sum FROM usage_hourly").fetchone()
    assert row["ttfb_sum"] == 100 and row["latency_sum"] == 400

def test_stats_model_timeline_top_models_and_points(stats):
    """按模型趋势：Top N 宽表点列，按总量降序；其余模型不出线。"""
    collector, query = stats
    base = 1_700_000_000
    # glm-5.2 共 4 次（最热）、kimi-k3 共 2 次、legacy-old 共 1 次（超出 Top 2）
    for _ in range(4):
        collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True, now=base)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True, now=base)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     now=base + 3600)
    collector.record(username="u", provider="codebuddy", model="legacy-old", ok=True,
                     now=base + 3600)
    collector.rollup_hourly()

    result = query.model_timeline(username="u", top=2)
    assert result["models"] == ["glm-5.2", "kimi-k3"]
    points = result["points"]
    assert len(points) == 2
    assert points[0] == {"hour": base // 3600 * 3600, "glm-5.2": 4, "kimi-k3": 1}
    assert points[1]["glm-5.2"] == 0 and points[1]["kimi-k3"] == 1
    # username 过滤：只统计指定用户
    assert query.model_timeline(username="nobody")["points"] == []

def test_stats_query_by_provider_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.by_provider(username="u", since=5000)[0]["requests"] == 1

def test_stats_overview_provider_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    collector.record(username="u", provider="codebuddy", model="m", ok=True)
    assert query.overview(username="u", provider="trae")["requests"] == 1

def test_stats_model_timeline_filters(stats):
    """按模型趋势：username / since 筛选生效。"""
    collector, query = stats
    collector.record(username="alice", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="bob", provider="trae", model="m2", ok=True, now=9000)
    collector.rollup_hourly()

    scoped = query.model_timeline(username="alice")
    assert scoped["models"] == ["m"]
    recent = query.model_timeline(since=3600)          # bob 的小时桶 7200 命中
    assert recent["models"] == ["m2"]

def test_stats_cached_tokens_overview_and_events(stats):
    """输入缓存命中：汇总（无上报则 None）与明细返回。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, cached_tokens=40)
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=50)
    overview = query.overview()
    assert overview["input_tokens"] == 150
    assert overview["cached_tokens"] == 40              # 只有一条上报 → SUM=40

    events = query.events()                          # 新→旧：后插入的未上报记录在前
    assert [e["cached_tokens"] for e in events["events"]] == [None, 40]

def test_stats_model_timeline_endpoint(admin_client):
    """model-timeline 端点透传聚合结果；metric 参数透传。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    collector.record(username="root", provider="trae", model="m", ok=True,
                     input_tokens=10, output_tokens=5, ttfb_ms=40)
    collector.rollup_hourly()
    body = client.get("/api/stats/model-timeline").json()
    assert body["models"] == ["m"] and body["points"]
    # metric 参数切换取值语义（默认请求次数 vs tokens 合计）
    tokens = client.get("/api/stats/model-timeline?metric=tokens").json()
    assert tokens["points"][0]["m"] == 15
    # 非法 metric 回退请求次数
    assert client.get("/api/stats/model-timeline?metric=bogus").json()["points"] == body["points"]

def test_stats_events_empty(stats):
    """空库：明细返回空页且无游标。"""
    _collector, query = stats
    assert query.events() == {"events": [], "next_before": None, "total": None}

def test_stats_events_pagination_and_filters(stats):
    """明细查询：新→旧、rowid 游标翻页不漏不重、筛选组合生效。"""
    collector, query = stats
    for index in range(5):
        collector.record(username="alice" if index % 2 else "bob",
                         provider="trae", model=f"m{index}", ok=bool(index % 2),
                         error_type=None if index % 2 else "rate_limit",
                         now=1000 + index)

    page1 = query.events(limit=2)
    assert [e["model"] for e in page1["events"]] == ["m4", "m3"]
    assert page1["next_before"] == page1["events"][-1]["rowid"]
    page2 = query.events(limit=2, before=page1["next_before"])
    assert [e["model"] for e in page2["events"]] == ["m2", "m1"]
    page3 = query.events(limit=2, before=page2["next_before"])
    assert [e["model"] for e in page3["events"]] == ["m0"]
    assert page3["next_before"] is None                     # 到底

    # 组合筛选：按用户（新→旧）；按时间下限
    assert [e["model"] for e in query.events(username="alice")["events"]] == ["m3", "m1"]
    assert [e["model"] for e in query.events(since=1003)["events"]] == ["m4", "m3"]

def test_stats_events_include_credential_name(repo):
    """明细带凭证 ID 与昵称：存在时 JOIN 昵称；已删除/无凭证时回退 NULL。"""
    credentials, db = repo
    collector, query = StatsCollector(db), StatsQuery(db)
    keep_id = credentials.add(provider="trae", credential_data={"accessToken": "t"},
                              nickname="主号")
    ghost_id = credentials.add(provider="trae", credential_data={"accessToken": "g"},
                               nickname="将删")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=keep_id)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="trae", model="m", ok=True)  # 无凭证
    assert credentials.delete(ghost_id) is True

    blank_id = credentials.add(provider="codearts", credential_data={}, nickname="")
    collector.record(username="u", provider="codearts", model="m", ok=True,
                     credential_id=blank_id)

    rows = {e["credential_id"]: e for e in
            query.events(include_credential_name=True)["events"]}
    assert rows[keep_id]["credential_name"] == "主号"
    assert rows[ghost_id]["credential_name"] is None        # 已删除 → 回退
    assert rows[None]["credential_name"] is None            # 无凭证
    assert rows[blank_id]["credential_name"] is None        # 空昵称 → 同回退

    # M7：默认（viewer 口径）不暴露共享池昵称
    default_rows = {e["credential_id"]: e for e in query.events()["events"]}
    assert default_rows[keep_id]["credential_name"] is None

def test_stats_events_endpoint_scope_and_clamp(admin_client):
    """明细端点：admin 可看他人；limit 被夹在 1..200。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    for index in range(3):
        collector.record(username="other", provider="trae", model=f"m{index}", ok=True)

    body = client.get("/api/stats/events").json()
    assert [e["model"] for e in body["events"]] == ["m2", "m1", "m0"]
    scoped = client.get("/api/stats/events", params={"username": "other"}).json()
    assert len(scoped["events"]) == 3
    limited = client.get("/api/stats/events", params={"limit": 2}).json()
    assert len(limited["events"]) == 2 and limited["next_before"] is not None
    assert len(client.get("/api/stats/events", params={"limit": 0}).json()["events"]) == 1
    huge = client.get("/api/stats/events", params={"limit": 10 ** 9}).json()
    assert len(huge["events"]) == 3

def test_stats_events_restrict_non_admin(tmp_path):
    """非 admin 只见自己的明细，指定他人用户名无效。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    app.state.stats_collector.record(username="alice", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="bob", provider="trae", model="m", ok=True)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        mine = client.get("/api/stats/events").json()
        assert [e["username"] for e in mine["events"]] == ["alice"]
        forced = client.get("/api/stats/events", params={"username": "bob"}).json()
        assert [e["username"] for e in forced["events"]] == ["alice"]
