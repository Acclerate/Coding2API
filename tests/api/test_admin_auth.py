"""管理台登录 / 登出 / 会话与 build_app 启动前置。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_login_success_sets_httponly_cookie(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/auth/login",
                               json={"username": "root", "password": "rootpw"})
        assert response.status_code == 200
        assert response.json() == {"username": "root", "is_admin": True,
                                   "is_operator": True,
                                   "role": "admin", "must_change_password": False}
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
        assert client.get("/api/auth/session").json()["username"] == "root"

def test_login_rejects_bad_password_and_unknown_user(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        for payload in ({"username": "root", "password": "wrong"},
                        {"username": "ghost", "password": "rootpw"},
                        {}):
            assert client.post("/api/auth/login", json=payload).status_code == 401

def test_logout_clears_session(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        assert client.post("/api/auth/logout").json() == {"ok": True}
        assert client.get("/api/auth/session").status_code == 401

def test_session_endpoint_requires_login(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").status_code == 401

def test_build_app_fails_without_users_file(tmp_path, monkeypatch):
    """文件缺失**且**库为空才是致命错误（B5：库非空时文件缺失可正常启动）。"""
    from src.auth.users import UsersFileError

    monkeypatch.setenv("USERS_FILE", str(tmp_path / "missing.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with pytest.raises(UsersFileError):
        build_app(settings)

def test_build_app_starts_without_users_file_when_db_has_users(tmp_path, monkeypatch):
    """库已有用户时，users.txt 缺失不再是启动障碍（部署忘挂文件不再等于不可用）。"""
    from src.auth.users import create_password_hash
    from src.db.conn import Database
    from src.db.migrate import apply_schema
    from src.db.repo import UserRepository

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role="admin", now=1)
    db.close()
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "missing.txt"))

    app = build_app(settings)
    with TestClient(app) as client:
        assert client.post("/api/auth/login",
                           json={"username": "root", "password": "rootpw"}).status_code == 200

def test_build_app_with_invalid_users_file_falls_back_to_db(tmp_path, monkeypatch):
    """文件存在但格式非法：当作没有文件，库里的用户说了算，而不是启动失败。

    users_file 必须显式传给 Settings：`Settings(_env_file=None)` 在构造时就读 OS env，
    若先构造再 monkeypatch.setenv，读到的仍是 conftest 的合法文件，这条测试就空了。
    """
    broken = tmp_path / "broken.txt"
    broken.write_text("this-line-has-no-colon\n", encoding="utf-8")
    monkeypatch.setenv("USERS_FILE", str(broken))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", USERS_FILE=str(broken))

    from src.auth.users import create_password_hash
    from src.db.conn import Database
    from src.db.migrate import apply_schema
    from src.db.repo import UserRepository

    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role="admin", now=1)
    db.close()

    app = build_app(settings)
    with TestClient(app) as client:
        logged = client.post("/api/auth/login",
                             json={"username": "root", "password": "rootpw"})
        assert logged.status_code == 200
        assert client.get("/api/auth/session").json()["role"] == "admin"
