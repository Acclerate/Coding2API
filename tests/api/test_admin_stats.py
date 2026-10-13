"""统计端点鉴权与作用域。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_stats_endpoints_scope_by_principal(admin_client):
    app, client = admin_client
    app.state.stats_collector.record(username="root", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="other", provider="trae", model="m", ok=True)
    app.state.stats_collector.rollup_hourly()
    overview = client.get("/api/stats/overview").json()
    assert overview["requests"] == 2                     # admin 看全局
    scoped = client.get("/api/stats/overview", params={"username": "other"}).json()
    assert scoped["requests"] == 1
    assert client.get("/api/stats/by-provider").json()["providers"][0]["requests"] == 2
    assert client.get("/api/stats/by-model").json()["models"][0]["requests"] == 2
    assert client.get("/api/stats/by-user").json()["users"] == [
        {"username": "other", "requests": 1, "ok_count": 1, "input_tokens": 0,
         "output_tokens": 0, "cached_tokens": None, "credit": None,
         "credit_estimated": False, "cost_usd": None, "cost_cny": None},
        {"username": "root", "requests": 1, "ok_count": 1, "input_tokens": 0,
         "output_tokens": 0, "cached_tokens": None, "credit": None,
         "credit_estimated": False, "cost_usd": None, "cost_cny": None},
    ]
    # 凭证分组读明细：本用例两条记录都没带 credential_id，故为空
    assert client.get("/api/stats/by-credential").json() == {"credentials": []}
    timeline = client.get("/api/stats/timeline").json()
    assert timeline["points"]
    # 渠道列动态取自数据（本用例只写了 trae），不再固定为两渠道
    assert list(timeline["points"][0].keys()) == ["hour", "trae"]

def test_stats_endpoints_restrict_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    app.state.stats_collector.record(username="alice", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="bob", provider="trae", model="m", ok=True)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        assert client.get("/api/stats/overview").json()["requests"] == 1
        # 非 admin 指定他人用户名无效，仍只看自己
        assert client.get("/api/stats/overview",
                          params={"username": "bob"}).json()["requests"] == 1

def test_stats_requires_session(admin_client):
    _app, client = admin_client
    client.cookies.clear()
    assert client.get("/api/stats/overview").status_code == 401
