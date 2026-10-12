"""CodeBuddy 国际版渠道（`codebuddy_intl`）接线与能力边界。

国际版与国内站**同协议**（同一 Keycloak realm、同一组 `/v2/*` `/v3/*` `/billing/*`
路径），所以本文件只测**接线与差异**，不重复测协议解析（那些在 test_m1b_codebuddy.py）：

- Settings 新字段与**独立**白名单（混发两站 Token 即泄露，必须测到）
- 端点解析的白名单拒绝路径
- 独立 pacer 与独立 OAuth 设备码流程
- 能力边界：`growth` / `activity` / `activity_scope` 为 None（未验证的端点上
  不做含不可逆动作的领取）
"""

from __future__ import annotations

import pytest

from src.config import (
    Settings,
    validate_codebuddy_intl_endpoint_allowed,
    validate_endpoint_allowed,
)
from src.engine.model_resolver import KNOWN_PROVIDERS
from src.provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider
from src.provider.codebuddy.intl import CodeBuddyIntlProvider
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.conftest import SECRET


class _MemoryStore:
    """最小 SettingsStore：只实现读写三动作。"""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.rows: dict[str, str] = dict(initial or {})

    def load(self) -> dict[str, str]:
        return dict(self.rows)

    def set(self, key: str, value: str, now: int | None = None) -> None:
        self.rows[key] = value

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)


def _settings(**overrides) -> Settings:
    defaults = {"_env_file": None, "APP_SECRET": SECRET, "DATA_DIR": "./data"}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------- Settings 字段

def test_intl_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.codebuddy_intl_api_endpoint == "https://www.codebuddy.ai"
    assert settings.codebuddy_intl_allowed == ("https://www.codebuddy.ai",)


def test_intl_allowed_parsing_ignores_blank_and_trailing_slash():
    settings = _settings(CODEBUDDY_INTL_ALLOWED_ENDPOINTS=(
        " https://www.codebuddy.ai/ , , "))
    assert settings.codebuddy_intl_allowed == ("https://www.codebuddy.ai",)
    assert _settings(CODEBUDDY_INTL_ALLOWED_ENDPOINTS="").codebuddy_intl_allowed == ()


def test_intl_whitelist_excludes_cn_site():
    """国际站白名单**不含**国内站主机；反之国内站白名单也不负责国际站。

    两站 Token 由不同网关签发，跨站发送就是凭证泄露——这是本渠道最要紧的
    一条不变量，所以显式测双向拒绝。
    """
    settings = _settings()
    cn = "https://copilot.tencent.com"
    intl = "https://www.codebuddy.ai"
    assert not validate_codebuddy_intl_endpoint_allowed(cn, settings)
    assert validate_codebuddy_intl_endpoint_allowed(intl, settings)
    # 国内站那条白名单里虽然列了国际站（历史遗留，写在那里供人查阅），
    # 但国际站的解析走的是自己那条校验，不会误用。
    assert validate_endpoint_allowed(intl, settings)


# --------------------------------------------------------------- 端点解析

def test_resolve_intl_endpoint_returns_configured_host():
    import src.main as main

    assert main._codebuddy_intl_endpoint(_settings()) == "https://www.codebuddy.ai"


def test_resolve_intl_endpoint_rejects_host_outside_whitelist():
    import src.main as main

    settings = _settings(CODEBUDDY_INTL_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="CODEBUDDY_INTL_API_ENDPOINT"):
        main._codebuddy_intl_endpoint(settings)


def test_resolve_intl_endpoint_rejects_cn_site_even_though_cn_whitelist_has_it():
    """把国内站主机填进国际站端点必须被拒——否则等于把国际站凭证发到国内站。"""
    import src.main as main

    settings = _settings(
        CODEBUDDY_INTL_API_ENDPOINT="https://copilot.tencent.com")
    with pytest.raises(ValueError, match="CODEBUDDY_INTL_API_ENDPOINT"):
        main._codebuddy_intl_endpoint(settings)


# --------------------------------------------------------------- 渠道注册

def test_intl_is_known_provider():
    assert "codebuddy_intl" in KNOWN_PROVIDERS


def test_build_app_registers_intl_channel(tmp_path):
    import src.main as main

    app = main.build_app(_settings(data_dir=str(tmp_path)))
    provider = app.state.services.registry["codebuddy_intl"]
    assert isinstance(provider, CodeBuddyIntlProvider)
    assert provider.id == "codebuddy_intl"
    assert provider.client.endpoint == "https://www.codebuddy.ai"
    assert provider.host() == "www.codebuddy.ai"


def test_build_app_keeps_cn_channel_on_cn_site(tmp_path):
    """两渠道并存：国内站渠道不能被国际站端点污染（旧凭证全靠这条）。"""
    import src.main as main

    app = main.build_app(_settings(data_dir=str(tmp_path)))
    registry = app.state.services.registry
    assert registry["codebuddy"].client.endpoint == "https://copilot.tencent.com"
    assert registry["codebuddy_intl"].client.endpoint == "https://www.codebuddy.ai"


def test_intl_channel_has_its_own_pacer(tmp_path):
    """独立节流器：共用会让两站请求排进同一条队列互相空等最小间隔。"""
    import src.main as main

    app = main.build_app(_settings(data_dir=str(tmp_path)))
    registry = app.state.services.registry
    assert registry["codebuddy_intl"].pacer is not registry["codebuddy"].pacer


def test_upstream_auth_registers_both_codebuddy_sites():
    """两站各一套设备码流程：登录域名不同，endpoint 不同。"""
    import src.main as main

    registry = {
        "codebuddy": CodeBuddyProvider(client=CodeBuddyClient()),
        "codebuddy_intl": CodeBuddyIntlProvider(client=CodeBuddyClient()),
    }
    flows = main._upstream_auth(registry, _settings())
    assert set(flows) == {"codebuddy", "codebuddy_intl"}
    assert flows["codebuddy"] is not flows["codebuddy_intl"]


# --------------------------------------------------------------- 能力边界

def test_intl_has_no_growth_or_activity():
    """成长中心与活跃上报在国际站**未验证**，一律关闭。

    成长中心含抽奖 / 连登兑换 / 盲盒等不可逆动作；调用方一律用
    `getattr(provider, "growth", None)` 探测，置 None 即自动跳过。
    """
    provider = CodeBuddyIntlProvider(client=CodeBuddyClient())
    assert getattr(provider, "growth", None) is None
    assert getattr(provider, "activity", None) is None
    assert getattr(provider, "activity_scope", None) is None


def test_cn_provider_keeps_growth_and_activity():
    """反向守卫：上一条的对照，国内站能力不能被误关。"""
    provider = CodeBuddyProvider(client=CodeBuddyClient())
    assert callable(provider.growth)
    assert callable(provider.activity)
    assert callable(provider.activity_scope)


def test_intl_keeps_chat_quota_models_checkin_refresh():
    """已验证存在的能力全部保留：与国内站同一套路径与协议。"""
    provider = CodeBuddyIntlProvider(client=CodeBuddyClient())
    for name in ("import_credential", "probe_quota", "list_models", "stream_chat",
                 "checkin", "checkin_status", "checkin_scope", "refresh",
                 "credential_from", "classify", "aclose", "host"):
        assert callable(getattr(provider, name)), name


def test_intl_pacer_key_is_scoped_by_channel():
    """节流桶键带渠道 id：两站即使共用账号标识也不会互相占位。"""
    from src.provider.codebuddy.client import _pacer_key
    from src.provider.codebuddy.credential import CodeBuddyCredential

    credential = CodeBuddyCredential.from_dict({"bearer_token": "tok"})
    cn = _pacer_key("codebuddy", credential)
    intl = _pacer_key("codebuddy_intl", credential)
    assert cn.startswith("codebuddy:")
    assert intl.startswith("codebuddy_intl:")
    assert cn != intl
    # 同一渠道同一凭证稳定（否则同账号多凭证无法共桶）
    assert _pacer_key("codebuddy", credential) == cn


# --------------------------------------------------------------- 热更间隔

def test_intl_min_interval_is_hot_updatable():
    assert "codebuddy_intl_chat_min_interval" in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    assert runtime.codebuddy_intl_chat_min_interval == 5
    runtime.set("codebuddy_intl_chat_min_interval", 0)
    assert runtime.codebuddy_intl_chat_min_interval == 0
    # 与国内站各自独立：改一个不影响另一个
    assert runtime.codebuddy_chat_min_interval == 5