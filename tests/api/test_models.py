"""模型目录出口 /v1/models：跨渠道合并、黑名单、排序与元数据。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """模块级模型缓存必须按测试清空，否则用例间互相污染。"""
    from src.provider.codebuddy.client import _MODEL_CACHE

    _MODEL_CACHE.clear()
    # `_days()` 的基准时刻同理：按测试重置，避免整套跑时基准越来越旧
    global _DAYS_BASE
    _DAYS_BASE = None
    yield
    _MODEL_CACHE.clear()

def test_default_registry_contains_both_providers(tmp_path):
    """双上游注册后 /v1/models 与 playground 模型一致，动态拉取失败回退静态表。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class FailingCB:
        id = "codebuddy"

        def list_models(self, _data):
            raise RuntimeError("dynamic fetch unavailable")

        def import_credential(self, raw):
            return raw

    class TraeStub:
        id = "trae"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={
        "trae": TraeStub(),
        "codebuddy": FailingCB(),
    })
    # 模型列表按渠道凭证加载：接入凭证后该渠道才参与（Q41）
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models",
                          headers={"Authorization": f"Bearer {key}"}).json()["data"]
    by_id = {item["id"]: item["providers"] for item in data}
    # CB 的 list_models 抛错 → 该上游被跳过（不拖垮整个列表），TRAE 正常返回
    assert by_id["glm-5.2"] == ["trae"]

def test_models_endpoint_skips_providers_without_credentials(tmp_path):
    """Q41：模型列表按渠道凭证加载——没凭证的渠道既不拉取也不展示。

    冷启动只有一个渠道接入时，另一个渠道的 list_models 根本不该被调用，
    Playground / `/v1/models` 也不应出现它的模型。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    calls: list[str] = []

    class Stub:
        def __init__(self, pid: str, model: str):
            self.id = pid
            self._model = model

        async def list_models(self, _data):
            from src.provider.base import Model

            calls.append(self.id)
            return [Model(id=self._model)]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={
        "trae": Stub("trae", "trae-only"),
        "codebuddy": Stub("codebuddy", "cb-only"),
    })
    # 只接 TRAE，CodeBuddy 无凭证
    trae_id = app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        ids = {m["id"] for m in client.get("/v1/models", headers=auth).json()["data"]}
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        playground = {m["id"] for m in client.get("/api/playground/models").json()["data"]}
        assert ids == {"trae-only"}
        assert playground == {"trae-only"}
        assert "codebuddy" not in calls              # CodeBuddy 未被拉取

        # 暂停唯一凭证 → 该渠道在列表里消失（不展示打不通的渠道）
        app.state.credentials.set_enabled(trae_id, False)
        assert client.get("/v1/models", headers=auth).json()["data"] == []

        # 恢复 → 立刻回到列表
        app.state.credentials.set_enabled(trae_id, True)
        assert {m["id"] for m in client.get(
            "/v1/models", headers=auth).json()["data"]} == {"trae-only"}

        # 硬禁用（会话失效）同样不展示；补一条新凭证立刻恢复
        from src.engine.scheduler import ErrorOutcome

        app.state.credentials.save_error(trae_id, ErrorOutcome(disabled=True))
        assert client.get("/v1/models", headers=auth).json()["data"] == []
        app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
        assert {m["id"] for m in client.get(
            "/v1/models", headers=auth).json()["data"]} == {"trae-only"}
    assert "codebuddy" not in calls

def test_models_cache_used_when_fetch_fails(tmp_path):
    """拉取成功后写入缓存；之后失败时用缓存兜底，/v1/models 保持完整。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class FailingCB:
        id = "codebuddy"

        def list_models(self, _data):
            raise RuntimeError("dynamic fetch unavailable")

        def import_credential(self, raw):
            return raw

    class FlakyTrae:
        id = "trae"
        fail = False

        async def list_models(self, _data):
            from src.provider.base import Model

            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2"), Model(id="DeepSeek-V4-Flash")]

        def import_credential(self, raw):
            return raw

    trae = FlakyTrae()
    app = build_app(settings, providers={"trae": trae, "codebuddy": FailingCB()})
    # CB 有凭证才会被尝试拉取（进而触发失败回退）；TRAE 同理（Q41）
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        # 第一次：拉取成功，缓存写入
        first = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in first} == {"glm-5.2", "deepseek-v4-flash"}
        cached_keys = app.state.services.model_list_cache["trae"].keys()
        assert cached_keys == {"glm-5.2", "deepseek-v4-flash"}

        # 第二次：拉取失败 → 用缓存兜底，列表不缺模型
        trae.fail = True
        second = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in second} == {"glm-5.2", "deepseek-v4-flash"}
        assert {item["id"] for item in second} and all(
            item["providers"] == ["trae"] for item in second)

        # 恢复后重新拉取成功，缓存刷新
        trae.fail = False
        third = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in third} == {"glm-5.2", "deepseek-v4-flash"}

def test_models_blocklist_filters_noise_and_old(tmp_path):
    """默认黑名单滤非用户模型（custom_model_*/subagent/summary/file_search_agent/
    default/hunyuan-image-*），MODEL_BLOCKLIST 覆盖后可再滤老模型；
    直连指定不受列表过滤影响。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class NoisyProvider:
        id = "trae"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2"), Model(id="kimi-k2.6"),
                    Model(id="custom_model_claude"),
                    Model(id="explore_sub_agent_v13"), Model(id="summary"),
                    Model(id="file_search_agent"), Model(id="default"),
                    Model(id="hunyuan-image-alpha")]

        def import_credential(self, raw):
            return raw

    app = build_app(settings, providers={"trae": NoisyProvider()})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 默认黑名单：噪音全滤（实测内部/不可用模型），老模型默认保留（是否滤由配置决定）
    assert "custom_model_claude" not in ids
    assert "explore_sub_agent_v13" not in ids
    assert "summary" not in ids
    assert "file_search_agent" not in ids
    assert "default" not in ids
    assert "hunyuan-image-alpha" not in ids
    assert {"glm-5.2", "kimi-k2.6"} <= ids

    # 覆盖黑名单：额外滤老模型（完全替换语义，需重写噪音规则）
    settings2 = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                         MODEL_BLOCKLIST="custom_model_*,*sub*agent*,summary,kimi-k2.6")
    app2 = build_app(settings2, providers={"trae": NoisyProvider()})
    app2.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key2 = app2.state.api_keys.create("root")["api_key"]
    with TestClient(app2) as client:
        ids2 = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key2}"}).json()["data"]}
    assert "kimi-k2.6" not in ids2 and "glm-5.2" in ids2

def test_models_blocklist_default_covers_retired_upstream_models(tmp_path):
    """默认黑名单含 2026-10-10 复测补入的三条「上游已下架」。

    `hy4-preview-x`（CB 400 11102）与同名的 `hy4-preview` 只差后缀，故必须
    写全名而不是 `hy4-preview*`；`qwen3.8-flash` 只命中 Qoder 的 qfmodel，
    CB/TRAE 的 `qwen3.8-max` 不受影响；`glyph-cluster` 按 kilo 展示名清洗出的
    归一键命中，不误伤同渠道其它模型。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="hy4-preview-x", name="Hy4 preview"),
                                        Model(id="hy4-preview", name="Hy4 preview")]),
        "qoder": Stub("qoder", [Model(id="qfmodel", name="Qwen3.8-Flash"),
                                Model(id="qmodel_38max", name="Qwen3.8-Max")]),
        "kilo": Stub("kilo", [Model(id="stealth/glyph-cluster",
                                    name="Stealth: Glyph Cluster (free)"),
                               Model(id="kilo-auto/free", name="Auto Free")]),
    })
    for pid in ("codebuddy", "qoder", "kilo"):
        app.state.credentials.add(provider=pid, credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    assert "hy4-preview-x" not in ids
    assert "qwen3.8-flash" not in ids
    assert "glyph-cluster" not in ids
    # 近邻模型不受牵连：同名不同 id 的 hy4-preview、qwen3.8-max、kilo-auto 都在
    assert {"hy4-preview", "qwen3.8-max", "kilo-auto"} <= ids

def test_models_blocklist_matches_normalized_id_and_display_name(tmp_path):
    """黑名单按**归一后的对外写法**匹配，且两边忽略大小写。

    对外 id 改为归一键后，用户照列表里看到的名字写黑名单必须生效——
    按 zen 归一键 `longcat-2.5-preview`、展示名派生的 `KIMI-K3`（**大小写
    与展示名不同**）都能滤掉；Qoder 的原代号 `kmodel_latest` 归一后是
    `kimi-k3`，故 `KIMI-K3`（忽略大小写）命中。同时老口径（原代号
    `custom_model_claude`）仍生效。未被列入的 `MiMo V2.5` 照常保留。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="KIMI-K3,longcat-2.5-preview,"
                                        "custom_model_claude")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "zen": Stub("zen", [Model(id="longcat-2.5-preview-free",
                                  name="LongCat 2.5 Preview"),
                            Model(id="mimo-v2.5-free", name="MiMo V2.5")]),
        "qoder": Stub("qoder", [Model(id="kmodel_latest", name="Kimi-K3"),
                                Model(id="custom_model_claude")]),
    })
    for provider_id in ("zen", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 归一键 / 展示名（大小写混合）/ 原代号三种写法命中即滤
    assert "longcat-2.5-preview" not in ids
    assert "kimi-k3" not in ids
    assert ids == {"mimo-v2.5"}   # 未列入黑名单的保留

def test_models_blocklist_hot_reload_applies_without_waiting_for_ttl(tmp_path):
    """MODEL_BLOCKLIST 热更后下一次 /v1/models 立即过滤，不受 TTL 缓存影响。

    回归点：缓存若存过滤后的结果，TTL（300s）内改黑名单不会生效，且被滤掉的
    模型会从「失败兜底缓存」里复活。现在缓存存未过滤表，过滤在出口现做。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class StubTrae:
        id = "trae"
        fail = False

        async def list_models(self, _data):
            from src.provider.base import Model

            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2"), Model(id="kimi-k2.6")]

        def import_credential(self, raw):
            return raw

    provider = StubTrae()
    app = build_app(settings, providers={"trae": provider})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        first = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in first} == {"glm-5.2", "kimi-k2.6"}
        # 缓存存的是**未过滤**表（两条都在），否则热更在 TTL 内无法生效
        assert set(app.state.services.model_list_cache["trae"]) == {"glm-5.2", "kimi-k2.6"}

        # TTL 未过（刚拉过），此时改黑名单
        client.put("/api/settings", json={"values": {"model_blocklist": "kimi-k2.6"}},
                   headers={"Referer": "http://testserver/"})
        second = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in second} == {"glm-5.2"}   # 立即生效

        # 上游随后失败：兜底缓存也不能让被滤模型复活
        provider.fail = True
        third = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in third} == {"glm-5.2"}

        # 清空黑名单（'' 与 null 等价 = 恢复默认）：被滤模型立刻回来
        client.put("/api/settings", json={"values": {"model_blocklist": ""}},
                   headers={"Referer": "http://testserver/"})
        fourth = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in fourth} == {"glm-5.2", "kimi-k2.6"}

def test_models_by_provider_rates_when_dual_upstream(tmp_path):
    """双上游同名模型倍率不同时，响应带 by_provider 按渠道给倍率。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, rate: float):
            self.id = pid
            self._rate = rate

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2", credit_rate=self._rate)]

        def import_credential(self, raw):
            return raw

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", 0.29), "trae": Stub("trae", 0.17)})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "a"})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        item = next(m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
            if m["id"] == "glm-5.2")
    assert item["providers"] == ["codebuddy", "trae"]
    assert item["credit_rate"] == 0.29          # 合并值：先到先填
    # by_provider 同时给出各渠道的 raw_id（渠道请求 key）与倍率
    assert item["by_provider"] == {
        "codebuddy": {"raw_id": "glm-5.2", "credit_rate": 0.29},
        "trae": {"raw_id": "glm-5.2", "credit_rate": 0.17}}

def test_models_name_passthrough_backfill_and_omit(tmp_path):
    """`name`（上游人类可读名）透传给前端；空名不占位、不产出空字段。

    Qoder 的 id 是 `qmodel_38max` 这类内部代号，仅显示 id 用户无法识别；
    上游 `display_name` 经 `Model.name` 透传后前端才有的可读名。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    # 上游没给可读名时（name=""）展示名从 id 现派生，两条渠道同 id 自然并成一条。
    # id 本身是纯 free 噪声（`free`）时派生不出名字，才不产出空的 "name" 字段。
    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="shared-model", name="")]),
        "trae": Stub("trae", [Model(id="shared-model", name="Shared Model"),
                              Model(id="free")]),
    })
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "a"})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]}
    assert set(data) == {"shared-model", "free"}       # 同 id 并成一条
    assert data["shared-model"]["name"] == "Shared Model"  # 取上游名并清洗
    assert "name" not in data["free"]                  # 派生不出名字则留空

def test_models_merge_by_readable_name_across_channels(tmp_path):
    """CB/TRAE/Qoder 同名不同 id 的模型并成一条，对外 id 用展示名的归一键。

    各渠道同一模型内部代号互不相同（`kimi-k3-1` / `kimi-k3` /
    `kmodel_latest`），只有可读名能对齐；请求转发到某渠道时再经别名表
    换回该渠道自己的原 id（Qoder 仍发 `kmodel_latest`）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy",
                          [Model(id="kimi-k3-1", name="Kimi-K3", credit_rate=0.3)]),
        "trae": Stub("trae", [Model(id="kimi-k3", name="Kimi-K3", credit_rate=0.2)]),
        "qoder": Stub("qoder",
                      [Model(id="kmodel_latest", name="Kimi-K3", credit_rate=1.4)]),
    })
    for provider_id in ("codebuddy", "trae", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]

    assert [m["id"] for m in data] == ["kimi-k3"]          # 三渠道并成一条
    entry = data[0]
    assert entry["providers"] == ["codebuddy", "qoder", "trae"]
    assert entry["name"] == "Kimi K3"      # 展示名清洗后与对外 id 同源
    # by_provider 带各渠道 raw_id（渠道请求 key，永不改动）与倍率
    assert entry["by_provider"] == {
        "codebuddy": {"raw_id": "kimi-k3-1", "credit_rate": 0.3},
        "qoder": {"raw_id": "kmodel_latest", "credit_rate": 1.4},
        "trae": {"raw_id": "kimi-k3", "credit_rate": 0.2}}
    # 别名表：对外 id 与原 id 都能映射回各渠道自己的上游 id
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["kimi-k3"] == "kmodel_latest"
    assert aliases["qoder"]["kmodel_latest"] == "kmodel_latest"
    assert aliases["codebuddy"]["kimi-k3"] == "kimi-k3-1"
    assert aliases["trae"]["kimi-k3"] == "kimi-k3"

def test_models_synonym_merges_qoder_deepseek_flash(tmp_path):
    """Qoder 的 `DeepSeek-Flash` 与别家的 `DeepSeek-V4.1-Flash` 并成一条。

    Qoder 上游名不带版本号（`dfmodel` 只叫 `DeepSeek-Flash`），归一键是
    `deepseek-flash`，单靠清洗与 `DeepSeek-V4.1-Flash` 对不上；`MODEL_SYNONYMS`
    把两者收敛到同一个规范键与规范展示名，请求 `deepseek-v4.1-flash` 也能
    路由到 Qoder（经别名表换回原代号 `dfmodel`）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="deepseek-v4.1-flash",
                                              name="Deepseek-V4.1-Flash",
                                              credit_rate=0.11)]),
        "trae": Stub("trae", [Model(id="deepseek-v4.1-flash",
                                    name="DeepSeek-V4.1-Flash", credit_rate=0.08)]),
        "qoder": Stub("qoder", [Model(id="dfmodel", name="DeepSeek-Flash",
                                      credit_rate=0.1)]),
    })
    for provider_id in ("codebuddy", "trae", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]

    assert [m["id"] for m in data] == ["deepseek-v4.1-flash"]    # 三渠道并成一条
    entry = data[0]
    assert entry["providers"] == ["codebuddy", "qoder", "trae"]
    assert entry["name"] == "DeepSeek V4.1 Flash"
    assert entry["by_provider"]["qoder"] == {"raw_id": "dfmodel", "credit_rate": 0.1}
    # 别名表：对外 id 与 Qoder 原代号都能映射到 dfmodel
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["deepseek-v4.1-flash"] == "dfmodel"
    assert aliases["qoder"]["dfmodel"] == "dfmodel"

def test_models_name_merge_includes_zen_and_guards_colliding_names(tmp_path):
    """六条渠道统一按归一键合并；同渠道内归一键重复的退回原 id。

    清洗规则统一后，zen 由 id 派生的名字（`LongCat 2.5 Preview` → `longcat-2.5-
    preview`）也能与其它渠道对齐，故不再排除在名合并之外——原先排除是因为
    派生名与别家不同源，现在同源了。

    仍要挡住同渠道重名：CodeBuddy 的 `hy4-preview` / `hy4-preview-x`（都叫
    「Hy4 Preview」）若都按名入键，其中一个会被同键覆盖而消失。

    黑名单置空：本用例只验合并口径，`hy4-preview-x` 已被默认黑名单滤掉
    （2026-10-10 实测 CB 400 11102 下架）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "zen": Stub("zen", [Model(id="longcat-2.5-preview-free",
                                  name="LongCat 2.5 Preview"),
                            Model(id="mimo-v2.5-free", name="MiMo V2.5")]),
        "codebuddy": Stub("codebuddy", [Model(id="hy4-preview", name="Hy4 preview"),
                                        Model(id="hy4-preview-x",
                                              name="Hy4 preview")]),
    })
    for provider_id in ("zen", "codebuddy"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {m["id"] for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 对外 id 一律是归一键：zen 的 free 尾缀被去掉；CB 重名条目因退回原代号
    # （展示名不唯一）也保持原 id。
    assert ids == {"longcat-2.5-preview", "mimo-v2.5",
                   "hy4-preview", "hy4-preview-x"}
    # 重名未按名入键：不得留下指向其中一个的歧义名字别名。两个原 id 各自
    # 映射回自己是正确的（用户按原 id 直连仍要能定位到本渠道）。
    assert app.state.services.model_aliases["codebuddy"] == {
        "hy4-preview": "hy4-preview", "hy4-preview-x": "hy4-preview-x"}

def test_models_merges_across_channels_when_duplicate_name_has_self_named_id(tmp_path):
    """渠道内展示名重名时，若 id 自洽的那个仍要**跨渠道合并**（2026-10-12）。

    上游把多个模型共用一个展示名是常态：CB 的 `hy4-preview` / `hy4-preview-x`
    都叫「Hy4 preview」，`hy3` / `hy3-x` 都叫「Hy3」。原来一律退回渠道限定键，
    代价是这些模型在**本渠道内**就断了跨渠道合并——国际站（无 `-x` 变体）的
    `hy4-preview` 本该与国内站合成一条，却因撞上被挤出去，对外多出
    `hy4-preview-codebuddy_intl` / `deepseek-v4.1-flash-codebuddy_intl` 这类后缀。

    改后：重名时用 id 归一值当合并键（重名者 id 不同故自然分开），其中 id 自洽
    的那个重新获得跨渠道合并能力；`-x` 变体保持独立条目，不会被合并掉。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="hy4-preview", name="Hy4 preview"),
                                        Model(id="hy4-preview-x", name="Hy4 preview")]),
        "codebuddy_intl": Stub("codebuddy_intl", [Model(id="hy4-preview",
                                                        name="Hy4 preview")]),
    })
    for provider_id in ("codebuddy", "codebuddy_intl"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        entries = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    # 裸名一条，同时挂两站——不再出现 -codebuddy_intl 后缀
    assert set(entries) == {"hy4-preview", "hy4-preview-x"}
    assert entries["hy4-preview"]["providers"] == ["codebuddy", "codebuddy_intl"]
    # -x 变体仍是国内站独有，不被合并掉
    assert entries["hy4-preview-x"]["providers"] == ["codebuddy"]
    # 两站都能按裸名定位到各自的原代号（国际站的原 id 恰同名）
    aliases = app.state.services.model_aliases
    assert aliases["codebuddy"]["hy4-preview"] == "hy4-preview"
    assert aliases["codebuddy_intl"]["hy4-preview"] == "hy4-preview"

def test_models_keeps_scoped_key_when_duplicate_names_have_no_self_named_id(tmp_path):
    """上一条的边界：重名组里**没有任何** id 自洽的模型时，仍退回渠道限定键。

    上游把两个不同模型都叫「Aquila」但 id 都与展示名无关（`orion` / `vega`），
    此时没有可用的归一键可当合并键——退回限定键才不会互相覆盖，行为与
    2026-10-12 改动前一致。
    """
    from src.api.models import _merge_key, _model_label, _scoped_key, normalize_model_key
    from src.provider.base import Model

    models = [Model(id="orion", name="Aquila"), Model(id="vega", name="Aquila")]
    label_counts: dict[str, int] = {}
    for model in models:
        key = normalize_model_key(_model_label(model))
        label_counts[key] = label_counts.get(key, 0) + 1
    duplicates = {k for k, c in label_counts.items() if c > 1}
    assert duplicates == {"aquila"}
    id_keys = {normalize_model_key(m.id) for m in models}
    # 两个 id 都与展示名归一键无关，故 id 自洽的候选不存在
    assert "aquila" not in id_keys
    for model in models:
        key, name_keyed = _merge_key(model, "trae", duplicates, id_keys)
        assert key == _scoped_key("trae", model.id)
        assert name_keyed is False

def test_models_id_named_model_in_duplicate_group_uses_id_key(tmp_path):
    """重名组里 id 自洽的那个走 id 键（不是限定键），因此仍可跨渠道合并。

    TRAE 实测口径：`sagitta` 与 `aquila` 都叫「Aquila」，其中 `aquila` 的 id
    归一后等于展示名归一键，故它拿到合并键 `aquila`，而 `sagitta` 单独成键。
    """
    from src.api.models import _merge_key, _model_label, normalize_model_key
    from src.provider.base import Model

    models = [Model(id="sagitta", name="Aquila"), Model(id="aquila", name="Aquila")]
    label_counts: dict[str, int] = {}
    for model in models:
        key = normalize_model_key(_model_label(model))
        label_counts[key] = label_counts.get(key, 0) + 1
    duplicates = {k for k, c in label_counts.items() if c > 1}
    id_keys = {normalize_model_key(m.id) for m in models}
    by_id = {m.id: _merge_key(m, "trae", duplicates, id_keys) for m in models}
    assert by_id["aquila"] == ("aquila", False)      # id 自洽 → 用 id 键，不登记别名
    assert by_id["sagitta"] == ("sagitta", False)

def test_models_single_channel_outward_id_is_normalized(tmp_path):
    """单渠道条目的对外 id 也是归一键（Qoder `kmodel_latest` → `kimi-k3`）。

    原代号只留在别名表与 `by_provider.raw_id`，转发时换回；用户按原代号直连
    仍能命中。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "qoder": Stub("qoder", [Model(id="kmodel_latest", name="Kimi-K3")]),
    })
    app.state.credentials.add(provider="qoder", credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
    assert [m["id"] for m in data] == ["kimi-k3"]      # 对外 id 用归一键
    assert data[0]["name"] == "Kimi K3"                # 展示名统一清洗
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["kimi-k3"] == "kmodel_latest"    # 对外 id → 原代号
    assert aliases["qoder"]["kmodel_latest"] == "kmodel_latest"  # 原代号也能直连
    assert aliases["qoder"]["kimi k3"] == "kmodel_latest"    # 原样的展示名也能直连

def test_models_name_merge_cross_channel_via_normalized_key(tmp_path):
    """清洗规则统一后 zen/kilo 也能与其它渠道并成一条。

    zen 的展示名原本由 id 本地派生、kilo 的是短标签，两条渠道都被排除在按名
    合并之外。现在归一规则对六条渠道一致：zen 的免费档尾缀与 TRAE 同名模型的
    id 归一到同一个键，kilo 的命名空间路径段也一样。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # zen 的免费档 `longcat-2.5-preview-free` 与 TRAE 的 `longcat-2.5-preview`
        # 归一到同一键（`longcat-2.5-preview`）→ 并成一条，对外 id 用该键
        "zen": Stub("zen", [Model(id="longcat-2.5-preview-free", name="")]),
        "trae": Stub("trae", [Model(id="longcat-2.5-preview", name="")]),
        # kilo 的 `kilo-auto/free` 归一到 `kilo-auto`，单渠道对外 id 用归一键
        "kilo": Stub("kilo", [Model(id="kilo-auto/free", name="")]),
    })
    for provider_id in ("zen", "trae", "kilo"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    assert set(data) == {"longcat-2.5-preview", "kilo-auto"}
    merged = data["longcat-2.5-preview"]
    assert merged["providers"] == ["trae", "zen"]
    assert merged["name"] == "LongCat 2.5 Preview"
    # 请求转发时仍发各渠道自己的原 key（raw key 永不改动）
    assert merged["by_provider"] == {
        "trae": {"raw_id": "longcat-2.5-preview"},
        "zen": {"raw_id": "longcat-2.5-preview-free"}}
    aliases = app.state.services.model_aliases
    assert aliases["zen"]["longcat-2.5-preview"] == "longcat-2.5-preview-free"
    assert aliases["zen"]["longcat-2.5-preview-free"] == "longcat-2.5-preview-free"
    # kilo 单渠道：对外 id 是归一键，原代号仍可直连
    assert aliases["kilo"]["kilo-auto"] == "kilo-auto/free"
    assert aliases["kilo"]["kilo-auto/free"] == "kilo-auto/free"

def test_models_sentinel_names_not_merged_across_channels(tmp_path):
    """哨兵名（`auto` / `default`）不跨渠道合并，各自单列。

    kilo 的 `kilo-auto/free`（上游名 `Auto Free`）与 Qoder 的 `auto` 都归一成
    `auto`，但一个是 kilo 的自动路由、一个是 Qoder 的默认模型，语义只在本渠道
    内成立。若合并，调度会把请求错误地路由到另一个上游。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "kilo": Stub("kilo", [Model(id="kilo-auto/free", name="Auto Free")]),
        "qoder": Stub("qoder", [Model(id="auto", name="Auto")]),
    })
    for provider_id in ("kilo", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = [m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]]
    assert {m["id"] for m in data} == {"kilo-auto", "auto"}
    assert all(len(m["providers"]) == 1 for m in data)   # 各渠道单列

def test_models_outward_id_disambiguated_on_collision(tmp_path):
    """对外 id 撞车时后来者退回原 id，保证对外 id 全局唯一。

    多渠道条目的对外 id 取展示名归一键，可能与另一条单渠道条目的原 id 撞上
    （这里 TRAE 的 `glm-5` 归一键 `glm-5`，而 kilo 有个原 id 就叫 `glm-5` 的
    模型）。撞车时两条 entry 会共用一个 id，前端选择器的 key 与别名表都会
    互相覆盖，故后来的那条退回自己的原 id。保留谁按渠道优先级：TRAE 优先于
    未排名的 kilo。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # trae（rank 1）多渠道条目归一键为 `glm-5`
        "trae": Stub("trae", [Model(id="glm", name="GLM 5")]),
        "zen": Stub("zen", [Model(id="glm-5", name="GLM 5")]),
        # kilo（未排名，靠后）单渠道条目的原 id 恰好也是 `glm-5` → 撞车
        "kilo": Stub("kilo", [Model(id="glm-5", name="Orphan GLM")]),
    })
    for provider_id in ("trae", "zen", "kilo"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    assert len(data) == 2
    # trae(rank 1) + zen 并成一条、保留归一键 glm-5；kilo 靠后，原 id 也撞上
    # → 换成归一后的展示名
    merged = data["glm-5"]
    assert merged["providers"] == ["trae", "zen"]
    disambiguated = data["orphan-glm"]
    assert disambiguated["providers"] == ["kilo"]
    # 别名表按渠道隔离：kilo 的模型只能发 kilo 的原 id
    aliases = app.state.services.model_aliases
    assert aliases["kilo"]["orphan-glm"] == "glm-5"
    assert aliases["trae"]["glm-5"] == "glm"
    assert aliases["zen"]["glm-5"] == "glm-5"

def test_models_outward_id_collision_falls_back_to_channel_suffix():
    """对外 id 撞车且候选被占时，依次退回「原代号归一键 → 加渠道名后缀」。

    entry 的对外 id 与主渠道原代号归一键都被先定稿的条目占掉时，退回加渠道名
    后缀的候选，保证对外 id 唯一。
    """
    from src.api.models import _disambiguate

    entries = [
        {"id": "x", "key": "kx", "providers": ["p1"], "raw_ids": {"p1": "x"}},
        {"id": "y", "key": "ky", "providers": ["p2"], "raw_ids": {"p2": "y"}},
        {"id": "k4", "key": "k4", "providers": ["p3"], "raw_ids": {"p3": "k4"}},
        # id（x）与主渠道原代号归一（y）都被占：退回 `y-p1`
        {"id": "x", "key": "k9", "providers": ["p1"], "raw_ids": {"p1": "y"}},
    ]
    _disambiguate(entries)
    assert [e["id"] for e in entries] == ["x", "y", "k4", "y-p1"]

def test_models_outward_id_collision_falls_back_to_numeric_suffix():
    """候选全被占时追加数字后缀（`_unique_suffix` 兜底），保证对外 id 唯一。"""
    from src.api.models import _disambiguate

    entries = [
        {"id": "x", "key": "kx", "providers": ["p1"], "raw_ids": {"p1": "x"}},
        {"id": "y", "key": "ky", "providers": ["p2"], "raw_ids": {"p2": "y"}},
        {"id": "y-p1", "key": "kz", "providers": ["p3"],
         "raw_ids": {"p3": "y-p1"}},
        {"id": "x-2", "key": "kw", "providers": ["p4"],
         "raw_ids": {"p4": "x-2"}},
        # 候选 x / y / y-p1 与后缀 x-2 全被占 → 用 x-3 兜底
        {"id": "x", "key": "k9", "providers": ["p1"], "raw_ids": {"p1": "y"}},
    ]
    _disambiguate(entries)
    assert [e["id"] for e in entries] == ["x", "y", "y-p1", "x-2", "x-3"]

def test_models_list_orders_cb_then_trae_first(tmp_path):
    """展示顺序：CB → TR → 其余渠道，组内按模型名字典序。

    多渠道模型按最高优先级渠道归位（含 CB 即进第一段）；这是纯展示排序，
    不影响调度选号（`model_resolver` 的 KNOWN_PROVIDERS 顺序）。
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

    app = build_app(settings, providers={
        # 故意让 zen 的模型名字典序最小、CB 的最大，证明排序按渠道权重而非名字
        "codebuddy": Stub("codebuddy", ["z-cb", "a-cb"]),
        "trae": Stub("trae", ["m-trae"]),
        "zen": Stub("zen", ["aaa-zen"]),
    })
    for provider_id in ("codebuddy", "trae", "zen"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = [item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]]
    assert ids == ["a-cb", "z-cb", "m-trae", "aaa-zen"]

def test_models_list_orders_dual_provider_by_highest_rank(tmp_path):
    """多渠道模型按最高优先级渠道归位：含 CB 进第一段，仅 TR 的进第二段。"""
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

    app = build_app(settings, providers={
        # cb-only 与 trae-only 同名 → 合并为多渠道，含 CB 归第一段
        "codebuddy": Stub("codebuddy", ["shared", "cb-only"]),
        "trae": Stub("trae", ["shared", "trae-only"]),
    })
    app.state.credentials.add(provider="codebuddy", credential_data={"t": "a"})
    app.state.credentials.add(provider="trae", credential_data={"t": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
    # shared（含 CB）与 cb-only 同段、按名排序；trae-only 进第二段
    assert [item["id"] for item in data] == ["cb-only", "shared", "trae-only"]
    assert next(item for item in data if item["id"] == "shared")["providers"] == [
        "codebuddy", "trae"]

def test_models_passthrough_reasoning_metadata(tmp_path):
    """B1.6：supports_reasoning / default_effort 随条目透传，且逐字段补缺。"""
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

    app = build_app(settings, providers={
        # trae 只给能力标志，codebuddy 补上默认档位（先到先填、后到补 None）
        "trae": Stub("trae", [Model(id="glm-5.3", supports_reasoning=True)]),
        "codebuddy": Stub("codebuddy", [Model(id="glm-5.3", supports_reasoning=True,
                                              default_effort="medium")]),
    })
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        item = next(m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
            if m["id"] == "glm-5.3")
    assert item["supports_reasoning"] is True
    assert item["default_effort"] == "medium"

def test_codebuddy_import_via_api(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        assert created.status_code == 200
        listed = client.get("/api/credentials").json()["credentials"]
        assert any(row["provider"] == "codebuddy" for row in listed)
        assert any(row["provider"] == "zen" for row in listed)  # 启动即种子
        assert any(row["provider"] == "kilo" for row in listed)  # 启动即种子
        bad = client.post("/api/credentials", json={"provider": "codebuddy",
                                                    "credential": {}})
        assert bad.status_code == 400
