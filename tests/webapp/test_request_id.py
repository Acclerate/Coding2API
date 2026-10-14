"""请求 ID：生成/校验/contextvar、中间件响应头、落库与统计查询透出。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.request_context import (
    REQUEST_ID_PATTERN,
    get_request_id,
    new_request_id,
    resolve_request_id,
    sanitize_request_id,
    set_request_id,
)
from src.webapp.request_id import RequestIdMiddleware


@pytest.fixture(autouse=True)
def _reset_request_id():
    """contextvar 在同一线程的用例之间会残留：每个用例前后都复原。"""
    set_request_id(None)
    yield
    set_request_id(None)


# ------------------------------------------------------------------ 纯函数

def test_new_request_id_is_32_hex():
    value = new_request_id()
    assert len(value) == 32
    assert REQUEST_ID_PATTERN.match(value)


def test_sanitize_accepts_safe_ids():
    for raw in ("abc", "a-b.c_d:e", "0123456789" * 12 + "12345678"):  # 128 位
        assert sanitize_request_id(raw) == raw


def test_sanitize_rejects_unsafe_ids():
    """客户端可写：换行/空格/引号/超长/空一律拒绝（否则能污染日志与 DB）。"""
    for raw in (None, "", "has space", "line\nbreak", "quote\"x", "slash/x",
                "a" * 129):
        assert sanitize_request_id(raw) is None


def test_resolve_keeps_valid_and_generates_otherwise():
    assert resolve_request_id("client-id") == "client-id"
    generated = resolve_request_id("bad id!")
    assert generated != "bad id!" and REQUEST_ID_PATTERN.match(generated)
    assert REQUEST_ID_PATTERN.match(resolve_request_id(None))


def test_contextvar_defaults_none_and_round_trips():
    # 无请求上下文（后台任务/脚本）时为 None
    assert get_request_id() is None
    set_request_id("rid-1")
    assert get_request_id() == "rid-1"
    set_request_id(None)        # 复原，避免污染同线程的后续用例


# ------------------------------------------------------------------ 中间件

def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ok")
    async def ok():
        return {"rid": get_request_id()}

    return app


def test_middleware_generates_and_echoes_request_id():
    with TestClient(_app()) as client:
        response = client.get("/ok")
    assert response.status_code == 200
    rid = response.headers["X-Request-ID"]
    assert REQUEST_ID_PATTERN.match(rid)
    assert response.json()["rid"] == rid        # 下游可见（contextvar 已设置）


def test_middleware_keeps_inbound_request_id():
    with TestClient(_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": "client-rid-1"})
    assert response.headers["X-Request-ID"] == "client-rid-1"
    assert response.json()["rid"] == "client-rid-1"


def test_middleware_discards_invalid_inbound_id():
    with TestClient(_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": "bad id"})
    rid = response.headers["X-Request-ID"]
    assert rid != "bad id" and REQUEST_ID_PATTERN.match(rid)


def test_middleware_ignores_non_http_scope():
    """非 http scope（如 lifespan）原样透传，不加头也不报错。"""
    import asyncio

    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = RequestIdMiddleware(app)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_middleware_appends_header_without_dropping_existing():
    """就地追加：下游已写的响应头不能被覆盖。"""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/headers")
    async def headers():
        from fastapi import Response

        return Response(status_code=201, headers={"X-Custom": "kept"})

    with TestClient(app) as client:
        response = client.get("/headers")
    assert response.headers["X-Custom"] == "kept"
    assert response.headers["X-Request-ID"]


# ------------------------------------------------------- 落库与统计查询

def test_record_persists_current_request_id(db):
    from src.stats.collector import StatsCollector
    from src.stats.query import StatsQuery

    collector, query = StatsCollector(db), StatsQuery(db)
    set_request_id("rid-persisted")
    collector.record(username="u", provider="trae", model="m", ok=True)
    rows = query.events(username="u")["events"]
    assert rows[0]["request_id"] == "rid-persisted"


def test_record_without_request_context_stores_null(db):
    """后台任务 / 脚本写入没有请求上下文 → request_id 为 NULL。"""
    from src.stats.collector import StatsCollector
    from src.stats.query import StatsQuery

    collector, query = StatsCollector(db), StatsQuery(db)
    assert get_request_id() is None
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert query.events(username="u")["events"][0]["request_id"] is None
