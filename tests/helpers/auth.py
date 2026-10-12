"""会话 Cookie、登录与 admin 客户端助手。

管理台端点测试普遍需要「建 app → 带 admin 会话 Cookie 的 TestClient」；
此前 4 个文件各写一份 `admin_client`，这里给一份等价实现。
"""

from __future__ import annotations

from src.auth.session import create_session_token
from src.config import Settings

from .constants import SECRET

__all__ = ["admin_session_cookie", "login", "set_session_cookie", "test_settings"]


def test_settings(tmp_path, **overrides) -> Settings:
    """构造隔离的 `Settings`：显式传值，绝不依赖本地 `.env`（CI 无 `.env`）。"""
    defaults: dict = {"_env_file": None, "APP_SECRET": SECRET,
                      "DATA_DIR": str(tmp_path), "ADMIN_USERNAMES": "root"}
    defaults.update(overrides)
    return Settings(**defaults)


def admin_session_cookie(username: str = "root", secret: str = SECRET) -> str:
    """admin 会话令牌（调用方自行写进 `coding2api_session` Cookie）。"""
    return create_session_token(username, secret)


def set_session_cookie(client, username: str = "root", secret: str = SECRET) -> None:
    """给 TestClient 挂上会话 Cookie。"""
    client.cookies.set("coding2api_session", create_session_token(username, secret))


def login(client, username: str, password: str):
    """走真实登录端点并断言成功，返回响应。"""
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response
