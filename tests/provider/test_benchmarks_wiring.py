"""OpenRouter 目录：端到端注入与汇率/周期热更。"""

from __future__ import annotations

import pytest

from src import main
from src.benchmarks import build_benchmark_table, save_catalog
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from tests.helpers.constants import SECRET
from tests.provider._benchmarks_support import OR_PAYLOAD as _OR_PAYLOAD
from tests.provider._benchmarks_support import benchmarks_app as _benchmarks_app
from tests.provider._benchmarks_support import price_app as _price_app

pytestmark = pytest.mark.real_upstream("openrouter")

def _seed_models(services, *, models: dict) -> None:
    """把渠道模型表塞进缓存并 publish 别名表，随后即可打 /v1/models。

    在 lifespan **之后**调用：启动预热会重拉各渠道（`_warm_model_list`），
    早于它填的缓存会被覆盖。trae 是否「已连接」由 `credential_providers`
    按凭证判定，这里顺带给该渠道加一条凭证，与线上「接入渠道」等价。
    """
    from src.api import models as models_api
    from src.provider.base import Model

    services.credentials.add(provider="trae", credential_data={"bearer_token": "t"})
    services.model_list_cache["trae"] = {
        key: (value if isinstance(value, Model) else Model(id=key, name=value))
        for key, value in models.items()}
    models_api.publish_aliases(services, {"trae"})



def _catalog_client(app):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    return client

def _create_api_key(client) -> str:
    """用 admin 会话造一把 API Key（/v1/models 走 API Key 鉴权）。"""
    from src.auth.session import create_session_token

    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    response = client.post("/api/api-keys", json={"name": "bench"}, headers={
        "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200, response.text
    return response.json()["api_key"]

def test_v1_models_and_playground_carry_benchmarks(tmp_path, monkeypatch):
    """/v1/models 与 /api/playground/models 都随条目透出分数（同一份表）。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={
        "glm-5.3": "GLM 5.3",
        "glm-5.3-flash": "GLM 5.3 Flash",
    })
    app.state.model_benchmarks.update(build_benchmark_table(_OR_PAYLOAD))
    with TestClient(app) as client:
        key = _create_api_key(client)
        data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        rows = {row["id"]: row for row in data["data"]}
        assert rows["glm-5.3"]["benchmarks"]["intelligence_index"] == 44.8
        # 前缀不算匹配：没有分数就不带该字段，而不是带一个错的值
        assert "benchmarks" not in rows["glm-5.3-flash"]

def test_models_endpoint_without_table_has_no_benchmarks(tmp_path, monkeypatch):
    """能力表为空（拉取失败 / 未装配）时，条目与加字段前完全一致。"""
    from fastapi.testclient import TestClient

    # 上游不可用（raw={}）→ 启动预热也拉不到，表保持空
    app = _benchmarks_app(tmp_path, monkeypatch, table={}, raw={})
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={"glm-5.3": "GLM 5.3"})
    with TestClient(app) as client:
        key = _create_api_key(client)
        data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        assert all("benchmarks" not in row for row in data["data"])

def test_playground_models_endpoint_requires_session(tmp_path, monkeypatch):
    """未登录访问 playground 列表仍 401（不带任何分数）。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app) as client:
        assert client.get("/api/playground/models").status_code == 401

def test_model_catalog_endpoint_merges_benchmarks(tmp_path, monkeypatch):
    """「模型列表」页按同一口径给行补分，无分不带字段。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    catalog = {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3",
                         "provider": "z-ai", "input": 1.0, "output": 2.0,
                         "cache_read": 0.1},
        "other/model": {"id": "other/model", "name": "No Score", "provider": "p",
                        "input": 0.5, "output": 1.0, "cache_read": 0.05},
    }
    save_catalog(str(tmp_path), {}, {}, catalog)
    # raw={} → 启动预热拿不到上游数据、失败退出，落盘明细目录得以保留（否则预热会
    # 用替身数据把它覆盖掉）；能力分表由 snapshot/helper 提供。
    app = _benchmarks_app(tmp_path, monkeypatch,
                          table=build_benchmark_table(_OR_PAYLOAD), raw={})
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        payload = client.get("/api/model-catalog").json()
    rows = {row["id"]: row for row in payload["models"]}
    assert rows["z-ai/glm-5.3"]["benchmarks"]["coding_index"] == 74.8
    assert "benchmarks" not in rows["other/model"]
    assert payload["saved_at"] is not None

def test_model_catalog_endpoint_lists_sorted_catalog(tmp_path):
    """带登录可读全量模型目录：按模型 id 升序，含汇率与快照时间。"""
    save_catalog(str(tmp_path), {}, {}, {
        "glm-5.2": {"id": "glm-5.2", "name": "GLM 5.2", "provider": "zhipuai",
                    "context": 200000, "input": 1.0, "output": 2.0,
                    "cache_read": 0.1},
        "a-model": {"id": "a-model", "name": None, "provider": "p",
                    "input": 0.5, "output": 1.5, "cache_read": 0.05},
    })
    app = _price_app(tmp_path, USD_CNY_RATE=7)
    with _catalog_client(app) as client:
        payload = client.get("/api/model-catalog").json()
        assert payload["count"] == 2
        assert payload["currency"] == "USD"
        assert payload["usd_cny_rate"] == 7
        assert payload["saved_at"] == app.state.price_saved_at
    assert [row["id"] for row in payload["models"]] == ["a-model", "glm-5.2"]
    assert payload["models"][1] == {
        "id": "glm-5.2", "name": "GLM 5.2", "provider": "zhipuai",
        "context": 200000, "input": 1.0, "output": 2.0, "cache_read": 0.1}

def test_model_catalog_endpoint_empty_table(tmp_path, monkeypatch):
    """无快照时回空表 + saved_at=None（页面显示空态，不是错误）。"""
    async def no_models(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", no_models)
    app = _price_app(tmp_path)
    with _catalog_client(app) as client:
        payload = client.get("/api/model-catalog").json()
    assert payload["models"] == [] and payload["count"] == 0
    assert payload["saved_at"] is None

def test_model_catalog_endpoint_requires_session(tmp_path):
    """未登录一律 401：模型列表页也要经过会话鉴权。"""
    from fastapi.testclient import TestClient

    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    app = _price_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/model-catalog").status_code == 401

# --------------------------------------------------------- 汇率热更

def test_usd_cny_rate_is_hot_setting(tmp_path):
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import load_runtime_settings

    db = Database(tmp_path / "r.sqlite3")
    apply_schema(db.connect())
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      USD_CNY_RATE=7.0)
    runtime = load_runtime_settings(config, RuntimeSettingsRepository(db))
    assert runtime.usd_cny_rate == 7.0
    runtime.set("usd_cny_rate", 6.5)
    assert runtime.usd_cny_rate == 6.5
    runtime.reset("usd_cny_rate")
    assert runtime.usd_cny_rate == 7.0
    db.close()

def test_openrouter_catalog_minutes_has_floor():
    from src.runtime_settings import HOT_BY_KEY

    spec = HOT_BY_KEY["openrouter_catalog_minutes"]
    assert spec.task == "openrouter_catalog"
    assert spec.floor == 60
    assert spec.minimum == 60
