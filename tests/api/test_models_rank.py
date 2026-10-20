"""模型目录出口 /v1/models：排序、按渠道费率与推理元数据透传。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_models_by_provider_rates_per_channel(tmp_path):
    """同名模型在两渠道倍率不同时，各自一条、各报各的倍率。

    2026-10-16 拆分后不再合并出「一条多渠道 + by_provider 细分」，而是每渠道
    一条（id 带渠道后缀），`by_provider` 仍给出该渠道的 raw_id 与倍率。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, rate: float):
            self.id = pid
            self._rate = rate

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.3", credit_rate=self._rate)]

        def import_credential(self, raw):
            return raw

    app = build_app(
        settings,
        providers={"codebuddy": Stub("codebuddy", 0.29), "trae": Stub("trae", 0.17)},
    )
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "a"})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }
    assert set(data) == {"glm-5.3-codebuddy", "glm-5.3-trae"}
    # by_provider 同时给出各渠道的 raw_id（渠道请求 key）与倍率，互不覆盖
    assert data["glm-5.3-codebuddy"]["by_provider"] == {
        "codebuddy": {"raw_id": "glm-5.3", "credit_rate": 0.29}
    }
    assert data["glm-5.3-trae"]["by_provider"] == {
        "trae": {"raw_id": "glm-5.3", "credit_rate": 0.17}
    }


def test_models_list_orders_cb_then_trae_first(tmp_path):
    """展示顺序：CB → TR → 其余渠道，组内按模型名字典序。

    每条按**自己的**渠道归位；纯展示排序，不影响调度选号（`model_resolver`
    的 KNOWN_PROVIDERS 顺序）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, ids: list[str]):
            self.id = pid
            self._ids = ids

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id=model_id) for model_id in self._ids]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(
        settings,
        providers={
            # 故意让 zen 的模型名字典序最小、CB 的最大，证明排序按渠道权重而非名字
            "codebuddy": Stub("codebuddy", ["z-cb", "a-cb"]),
            "trae": Stub("trae", ["m-trae"]),
            "zen": Stub("zen", ["aaa-zen"]),
        },
    )
    for provider_id in ("codebuddy", "trae", "zen"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = [
            item["id"]
            for item in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        ]
    assert ids == ["a-cb-codebuddy", "z-cb-codebuddy", "m-trae-trae", "aaa-zen-zen"]


def test_models_list_orders_each_entry_by_own_channel_rank(tmp_path):
    """同名模型拆开后各按自己的渠道归位：CB 条目在第一段、TR 条目在第二段。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, ids: list[str]):
            self.id = pid
            self._ids = ids

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id=model_id) for model_id in self._ids]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(
        settings,
        providers={
            # cb-only 与 trae-only 同名（`shared`）→ 各成一条，各归各段
            "codebuddy": Stub("codebuddy", ["shared", "cb-only"]),
            "trae": Stub("trae", ["shared", "trae-only"]),
        },
    )
    app.state.credentials.add(provider="codebuddy", credential_data={"t": "a"})
    app.state.credentials.add(provider="trae", credential_data={"t": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}
        ).json()["data"]
    # CB 的 shared / cb-only 在第一段（rank 0，组内按名序）；trae 的两条在第二段
    assert [item["id"] for item in data] == [
        "cb-only-codebuddy",
        "shared-codebuddy",
        "shared-trae",
        "trae-only-trae",
    ]


def test_models_passthrough_reasoning_metadata(tmp_path):
    """supports_reasoning / default_effort 随条目透传（各渠道各自一份，不跨渠道补缺）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return self._models

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    app = build_app(
        settings,
        providers={
            "trae": Stub("trae", [Model(id="glm-5.3", supports_reasoning=True)]),
            "codebuddy": Stub(
                "codebuddy",
                [Model(id="glm-5.3", supports_reasoning=True, default_effort="medium")],
            ),
        },
    )
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }
    assert data["glm-5.3-trae"]["supports_reasoning"] is True
    assert "default_effort" not in data["glm-5.3-trae"]  # 没给就不透传空串
    assert data["glm-5.3-codebuddy"]["default_effort"] == "medium"
