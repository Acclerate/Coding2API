"""模型目录出口 /v1/models：每模型一条、对外 id 归一 + 渠道后缀与别名表。

2026-10-16 起**不跨渠道合并**：同一模型在三条渠道都有时列表里就是三条，
对外 id = `归一键-渠道`（`kimi-k3-qoder`）——看到名字即知来源厂商，请求该
id 即钉死该渠道。裸归一键 / 展示名 / 原代号仍登记进别名表，可直接请求
（裸名按登记渠道扇出，旧口径的自动选号语义）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_models_name_passthrough_and_omit(tmp_path):
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

    # 上游没给可读名时（name=""）展示名从 id 现派生；id 本身是纯 free 噪声
    # （`free`）时派生不出名字，才不产出空的 "name" 字段。
    app = build_app(
        settings,
        providers={
            "codebuddy": Stub("codebuddy", [Model(id="shared-model", name="")]),
            "trae": Stub(
                "trae",
                [Model(id="shared-model", name="Shared Model"), Model(id="free")],
            ),
        },
    )
    for provider_id in ("codebuddy", "trae"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }
    # 每渠道一条、id 带渠道后缀；同名不再并成一条
    assert set(data) == {"shared-model-codebuddy", "shared-model-trae", "free-trae"}
    assert data["shared-model-codebuddy"]["name"] == "Shared Model"  # 从 id 派生
    assert data["shared-model-trae"]["name"] == "Shared Model"  # 取上游名
    assert "name" not in data["free-trae"]  # 派生不出名字则留空


def test_models_one_entry_per_provider_and_request_pins_channel(tmp_path):
    """CB/TRAE/Qoder 同名不同 id 的模型各成一条，对外 id 带渠道后缀。

    各渠道同一模型内部代号互不相同（`kimi-k3-1` / `kimi-k3` /
    `kmodel_latest`），只有可读名能对齐；拆开后每条钉死自己的渠道——请求
    `kimi-k3-qoder` 经别名表换回 `kmodel_latest`，**永远走 qoder**，故障一眼
    看出是哪家。三家各自独立计费倍率。
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

    app = build_app(
        settings,
        providers={
            "codebuddy": Stub(
                "codebuddy", [Model(id="kimi-k3-1", name="Kimi-K3", credit_rate=0.3)]
            ),
            "trae": Stub(
                "trae", [Model(id="kimi-k3", name="Kimi-K3", credit_rate=0.2)]
            ),
            "qoder": Stub(
                "qoder", [Model(id="kmodel_latest", name="Kimi-K3", credit_rate=1.4)]
            ),
        },
    )
    for provider_id in ("codebuddy", "trae", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}
        ).json()["data"]

    # 展示顺序按渠道优先级：CB → TR → Qoder（`_PROVIDER_RANK`）
    assert [m["id"] for m in data] == [
        "kimi-k3-codebuddy",
        "kimi-k3-trae",
        "kimi-k3-qoder",
    ]
    entry = next(m for m in data if m["id"] == "kimi-k3-qoder")
    assert entry["providers"] == ["qoder"]
    assert entry["name"] == "Kimi K3"  # 展示名统一清洗
    # by_provider 带渠道 raw_id（渠道请求 key，永不改动）与倍率
    assert entry["by_provider"] == {
        "qoder": {"raw_id": "kmodel_latest", "credit_rate": 1.4}
    }
    # 别名表按渠道隔离：对外 id 只登记在自己的渠道里（请求即钉死该渠道）
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["kimi-k3-qoder"] == "kmodel_latest"
    assert "kimi-k3-qoder" not in aliases["codebuddy"]
    # 裸归一键 / 原代号 / 展示名三种旧写法仍能直连（裸名按登记渠道扇出）
    assert aliases["qoder"]["kimi-k3"] == "kmodel_latest"
    assert aliases["qoder"]["kmodel_latest"] == "kmodel_latest"
    assert aliases["qoder"]["kimi k3"] == "kmodel_latest"
    assert aliases["codebuddy"]["kimi-k3"] == "kimi-k3-1"


def test_models_synonym_canonicalizes_key_and_display(tmp_path):
    """Qoder 的 `DeepSeek-Flash` 与别家的 `DeepSeek-V4.1-Flash` 收敛到同一主体。

    Qoder 上游名不带版本号（`dfmodel` 只叫 `DeepSeek-Flash`），归一键是
    `deepseek-flash`，单靠清洗与 `DeepSeek-V4.1-Flash` 对不上；`MODEL_SYNONYMS`
    把两者收敛到同一个规范键与规范展示名——拆开后各渠道仍各成一条，但
    主体统一为 `deepseek-v4.1-flash`（id `deepseek-v4.1-flash-qoder`），请求
    `deepseek-v4.1-flash-qoder` 能路由到 Qoder（经别名表换回 `dfmodel`）。
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

    app = build_app(
        settings,
        providers={
            "codebuddy": Stub(
                "codebuddy",
                [
                    Model(
                        id="deepseek-v4.1-flash",
                        name="Deepseek-V4.1-Flash",
                        credit_rate=0.11,
                    )
                ],
            ),
            "trae": Stub(
                "trae",
                [
                    Model(
                        id="deepseek-v4.1-flash",
                        name="DeepSeek-V4.1-Flash",
                        credit_rate=0.08,
                    )
                ],
            ),
            "qoder": Stub(
                "qoder", [Model(id="dfmodel", name="DeepSeek-Flash", credit_rate=0.1)]
            ),
        },
    )
    for provider_id in ("codebuddy", "trae", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }

    assert set(data) == {
        "deepseek-v4.1-flash-codebuddy",
        "deepseek-v4.1-flash-qoder",
        "deepseek-v4.1-flash-trae",
    }
    entry = data["deepseek-v4.1-flash-qoder"]
    assert entry["name"] == "DeepSeek V4.1 Flash"  # 异名收敛到规范展示名
    assert entry["by_provider"]["qoder"] == {"raw_id": "dfmodel", "credit_rate": 0.1}
    # 别名表：对外 id 与 Qoder 原代号都能映射到 dfmodel
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["deepseek-v4.1-flash-qoder"] == "dfmodel"
    assert aliases["qoder"]["dfmodel"] == "dfmodel"


def test_models_duplicate_display_names_fall_back_to_raw_key(tmp_path):
    """同渠道内展示名重名的模型退回原代号归一键，展示名不入别名。

    上游把多个不同模型共用一个展示名是常态：CodeBuddy 的 `hy4-preview` /
    `hy4-preview-x` 都叫「Hy4 Preview」，若都按名入键，其中一个会被同键覆盖
    而消失。重名者靠**不同的 id 归一值**自然分开（`hy4-preview` /
    `hy4-preview-x`），歧义的展示名不得登记成别名（会指向错误模型）。

    黑名单置空：本用例只验命名口径，`hy4-preview-x` 已被默认黑名单滤掉
    （2026-10-10 实测 CB 400 11102 下架）。
    """
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path), MODEL_BLOCKLIST=""
    )

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(
        settings,
        providers={
            "zen": Stub(
                "zen",
                [
                    Model(id="longcat-2.5-preview-free", name="LongCat 2.5 Preview"),
                    Model(id="mimo-v2.5-free", name="MiMo V2.5"),
                ],
            ),
            "codebuddy": Stub(
                "codebuddy",
                [
                    Model(id="hy4-preview", name="Hy4 preview"),
                    Model(id="hy4-preview-x", name="Hy4 preview"),
                ],
            ),
        },
    )
    for provider_id in ("zen", "codebuddy"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {
            m["id"]
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }
    # 对外 id = 归一键-渠道：zen 的 free 尾缀被去掉；CB 重名条目退回原代号归一值
    assert ids == {
        "longcat-2.5-preview-zen",
        "mimo-v2.5-zen",
        "hy4-preview-codebuddy",
        "hy4-preview-x-codebuddy",
    }
    # 重名未按名入键：不得留下指向其中一个的歧义名字别名。两个原 id 各自
    # 映射回自己是正确的（用户按原 id 直连仍要能定位到本渠道）。
    assert app.state.services.model_aliases["codebuddy"] == {
        "hy4-preview-codebuddy": "hy4-preview",
        "hy4-preview": "hy4-preview",
        "hy4-preview-x-codebuddy": "hy4-preview-x",
        "hy4-preview-x": "hy4-preview-x",
    }


def test_entry_base_prefers_unique_display_name():
    """`_entry_base`：唯一展示名入键；哨兵 / 空名 / 重名退回原代号归一键。"""
    from src.api.model_merge import _entry_base, _model_label, normalize_model_key
    from src.provider.base import Model

    # 唯一展示名 → 主体 = 展示名归一键，按名入键
    kimi = Model(id="kmodel_latest", name="Kimi-K3")
    assert _entry_base(kimi, set()) == ("kimi-k3", True)

    models = [Model(id="orion", name="Aquila"), Model(id="vega", name="Aquila")]
    duplicates = {normalize_model_key(_model_label(m)) for m in models}
    assert duplicates == {"aquila"}
    # 重名组里没有 id 自洽的模型（orion / vega）：都退回原代号归一键
    for model in models:
        assert _entry_base(model, duplicates) == (normalize_model_key(model.id), False)

    # 重名组里 id 自洽的那个（aquila）主体不变（仍是归一值），但同样不按名入键
    pair = [Model(id="sagitta", name="Aquila"), Model(id="aquila", name="Aquila")]
    dup = {normalize_model_key(_model_label(m)) for m in pair}
    assert _entry_base(pair[0], dup) == ("sagitta", False)
    assert _entry_base(pair[1], dup) == ("aquila", False)

    # 哨兵名（auto / default）语义只在渠道内成立 → 原代号归一键
    sentinel = Model(id="kilo-auto/free", name="Auto Free")
    assert _entry_base(sentinel, set()) == ("kilo-auto", False)

    # 展示名清洗后为空（纯 free 噪声）→ 原代号归一键
    assert _entry_base(Model(id="free"), set()) == ("free", False)


def test_models_single_channel_by_provider_exposes_raw_id(tmp_path):
    """单渠道条目的对外 id 也是 `归一键-渠道`（Qoder `kmodel_latest` →
    `kimi-k3-qoder`），且 `by_provider` 一律带 raw_id（排障看得到实际发的 key）。

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

    app = build_app(
        settings,
        providers={
            "qoder": Stub("qoder", [Model(id="kmodel_latest", name="Kimi-K3")]),
        },
    )
    app.state.credentials.add(provider="qoder", credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}
        ).json()["data"]
    assert [m["id"] for m in data] == ["kimi-k3-qoder"]  # 归一键 + 渠道后缀
    assert data[0]["name"] == "Kimi K3"  # 展示名统一清洗
    assert data[0]["by_provider"] == {"qoder": {"raw_id": "kmodel_latest"}}
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["kimi-k3-qoder"] == "kmodel_latest"  # 对外 id → 原代号
    assert aliases["qoder"]["kmodel_latest"] == "kmodel_latest"  # 原代号也能直连
    assert aliases["qoder"]["kimi-k3"] == "kmodel_latest"  # 裸归一键也能直连
    assert aliases["qoder"]["kimi k3"] == "kmodel_latest"  # 展示名也能直连


def test_models_zen_free_and_kilo_namespace_keys_normalized(tmp_path):
    """zen 免费档尾缀与 kilo 命名空间前缀剥掉后当 id 主体，再拼渠道后缀。

    zen 的 `longcat-2.5-preview-free` 与 kilo 的 `kilo-auto/free` 都是同一套
    归一规则：`longcat-2.5-preview` / `kilo-auto`，各渠道各成一条。
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

    app = build_app(
        settings,
        providers={
            "zen": Stub("zen", [Model(id="longcat-2.5-preview-free", name="")]),
            "trae": Stub("trae", [Model(id="longcat-2.5-preview", name="")]),
            "kilo": Stub("kilo", [Model(id="kilo-auto/free", name="")]),
        },
    )
    for provider_id in ("zen", "trae", "kilo"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }

    assert set(data) == {
        "longcat-2.5-preview-zen",
        "longcat-2.5-preview-trae",
        "kilo-auto-kilo",
    }
    assert data["longcat-2.5-preview-zen"]["name"] == "LongCat 2.5 Preview"
    # 请求转发时仍发各渠道自己的原 key（raw key 永不改动）
    assert data["longcat-2.5-preview-zen"]["by_provider"] == {
        "zen": {"raw_id": "longcat-2.5-preview-free"}
    }
    aliases = app.state.services.model_aliases
    assert aliases["zen"]["longcat-2.5-preview-zen"] == "longcat-2.5-preview-free"
    assert aliases["zen"]["longcat-2.5-preview-free"] == "longcat-2.5-preview-free"
    # kilo：对外 id 带渠道后缀，原代号仍可直连
    assert aliases["kilo"]["kilo-auto-kilo"] == "kilo-auto/free"
    assert aliases["kilo"]["kilo-auto/free"] == "kilo-auto/free"


def test_models_sentinel_names_scoped_per_channel(tmp_path):
    """哨兵名（`auto` / `default`）展示名只在渠道内成立，id 退回原代号归一键。

    kilo 的 `kilo-auto/free`（上游名 `Auto Free`）与 Qoder 的 `auto` 都归一成
    `auto`，但一个是 kilo 的自动路由、一个是 Qoder 的默认模型，语义只在本渠道
    内成立——对外 id 退回原代号归一键（`kilo-auto-kilo` / `auto-qoder`），
    且歧义的展示名不入别名。
    """
    # 默认黑名单含 `auto`（会滤掉这两个哨兵条目），本用例测合并语义而非默认
    # 黑名单内容，显式置空隔离默认值漂移
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path), MODEL_BLOCKLIST=""
    )

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(
        settings,
        providers={
            "kilo": Stub("kilo", [Model(id="kilo-auto/free", name="Auto Free")]),
            "qoder": Stub("qoder", [Model(id="auto", name="Auto")]),
        },
    )
    for provider_id in ("kilo", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = [
            m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        ]
    assert {m["id"] for m in data} == {"kilo-auto-kilo", "auto-qoder"}
    assert all(len(m["providers"]) == 1 for m in data)  # 各渠道单列
    # 展示名 "Auto" 有歧义（两家各自的自动档）→ 不入别名；裸主体仍可直连
    aliases = app.state.services.model_aliases
    assert "auto" not in aliases["kilo"]
    assert aliases["kilo"]["kilo-auto"] == "kilo-auto/free"
    assert aliases["qoder"]["auto"] == "auto"


def test_models_outward_id_falls_back_to_raw_key_on_collision(tmp_path):
    """同渠道内归一键撞车时后来者退回原代号归一键，对外 id 唯一。

    兜底/哨兵条目的归一主体可能恰好等于另一条正常条目的展示名归一键（kilo
    一个模型的原代号归一后恰好等于另一个模型的展示名归一键）。撞车时两条
    entry 共用一个 id 会让前端选择器的 `key` 与该渠道的别名表互相覆盖，故
    后来的那条退回自己的原代号归一键。保留谁按 `_sort_key` 序（先定稿优先）。
    """
    # 默认黑名单含 `glm-5`（夹具主体正是它），显式置空隔离默认值漂移
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path), MODEL_BLOCKLIST=""
    )

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(
        settings,
        providers={
            # `glm-5`（纯噪声名 → 退回原代号归一）先定稿占住主体 `glm-5`；
            # `zzz` 的展示名「GLM 5」归一后也是 `glm-5` → 撞车退回原代号 `zzz`
            "kilo": Stub(
                "kilo", [Model(id="glm-5", name="Free"), Model(id="zzz", name="GLM 5")]
            ),
        },
    )
    app.state.credentials.add(provider="kilo", credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }

    assert set(data) == {"glm-5-kilo", "zzz-kilo"}
    aliases = app.state.services.model_aliases
    assert aliases["kilo"]["zzz"] == "zzz"  # 退回的原代号归一键仍可直连
    assert aliases["kilo"]["zzz-kilo"] == "zzz"
    assert aliases["kilo"]["glm-5"] == "glm-5"


def test_models_outward_id_numeric_when_fallback_taken(tmp_path):
    """撞车且原代号归一键也被占时追加数字后缀（`_unique_suffix` 兜底）。

    `foo` / `foo-free` / `foo:free` 展示名同为「Foo」（重名 → 都退回原代号
    归一键），而三者剥掉 free 标记后归一值都是 `foo`——第二个候选也被占，
    只能数字兜底；`foo-2` 也被占后 `_unique_suffix` 继续自增到 `foo-3`，
    保证对外 id 唯一。
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

    app = build_app(
        settings,
        providers={
            "kilo": Stub(
                "kilo",
                [
                    Model(id="foo", name="Foo"),
                    Model(id="foo-free", name="Foo"),
                    Model(id="foo:free", name="Foo"),
                ],
            ),
        },
    )
    app.state.credentials.add(provider="kilo", credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }

    assert set(data) == {"foo-kilo", "foo-2-kilo", "foo-3-kilo"}
    aliases = app.state.services.model_aliases
    assert aliases["kilo"]["foo-2"] == "foo-free"  # 数字后缀主体也可直连
    assert aliases["kilo"]["foo-2-kilo"] == "foo-free"
    assert aliases["kilo"]["foo-3-kilo"] == "foo:free"


def test_models_outward_id_never_contains_at_sign(tmp_path):
    """契约：`/v1/models` 的对外 id 永不含 `@`——即使上游 id/名字带 @。

    `@` 是**请求侧**钉渠道语法（`model_resolver.resolve` 对 `model@provider`
    做 rpartition 劈分）：对外 id 若含 `@`，客户端照列表选中的名字发请求会被
    劈成「模型名 + 未知渠道」而 400（或更糟，劈进某个真实渠道名导致误路由）。

    保证机制是对外 id 的主体产地 `normalize_model_key`：slug 杂字符集
    （非 `\\w.+-` 一律替换成 `-`，见 `naming._JUNK_CHARS`）不认 `@`，故无论
    从 id 还是展示名归一（展示名清洗可能截断成 `Foo@` 这类残形），产出的
    主体都不含 `@`；渠道后缀本身是已知渠道 id，也不含 `@`。此测试把该隐式
    行为显式化：将来谁改了清洗正则、或新增绕过归一的 id 生成路径，这里立即
    红灯。渠道原始代号不受影响（`raw_id` 永不改动，@ 原样保留，经别名表在
    转发时使用）。
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

    app = build_app(
        settings,
        providers={
            # id 与展示名都带 @：对外 id 两条路（id 归一 / 展示名归一）都要被打掉
            "codebuddy": Stub("codebuddy", [Model(id="foo@bar", name="Foo@Bar")]),
            "trae": Stub(
                "trae",
                [Model(id="m@x", name="Foo@Bar"), Model(id="solo@trae", name="")],
            ),
        },
    )
    for provider_id in ("codebuddy", "trae"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {
            m["id"]: m
            for m in client.get(
                "/v1/models", headers={"Authorization": f"Bearer {key}"}
            ).json()["data"]
        }

    assert all("@" not in model_id for model_id in data)
    # 带敌意名字的模型没有消失：@ 被打掉后照常列出（展示名清洗截断成 `Foo@`
    # 这类残形，归一后主体是 `foo`），每渠道各一条
    assert set(data) == {"foo-codebuddy", "foo-trae", "solo-trae"}
    # 渠道原始代号原样透出（raw key 永不改动），@ 只留在转发层
    assert data["foo-codebuddy"]["by_provider"]["codebuddy"]["raw_id"] == "foo@bar"
    # 别名表按对外 id（无 @）登记，flat 名请求能落到原代号
    aliases = app.state.services.model_aliases
    assert aliases["codebuddy"]["foo-codebuddy"] == "foo@bar"
    assert aliases["codebuddy"]["foo"] == "foo@bar"
