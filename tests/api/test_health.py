"""/healthz 池计数。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.main import build_app
from tests.helpers.auth import isolated_settings as _settings


def test_healthz_reports_pool_counts(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["service"] == "coding2api"
    assert body["version"]
    counts = body["credentials"]
    # 默认装配会种子 zen / kilo 两条无凭证虚拟凭证（可选中 → ready）
    assert counts == {"total": 2, "ready": 2, "cooling": 0, "paused": 0, "disabled": 0}
    # 老端点保留
    assert client.get("/health").json() == {"status": "ok"}

def test_healthz_counts_partition_the_pool(tmp_path):
    """四类计数互斥且合计 = total；口径与调度器 is_selectable 一致。"""
    import sqlite3

    settings = _settings(tmp_path)
    app = build_app(settings)
    repo = app.state.credentials
    repo.add(provider="trae", credential_data={"accessToken": "a"})
    paused = repo.add(provider="trae", credential_data={"accessToken": "b"})
    cooling = repo.add(provider="trae", credential_data={"accessToken": "c"})
    broken = repo.add(provider="trae", credential_data={"accessToken": "d"})
    repo.set_enabled(paused, False)
    # 硬禁用/冷却没有公开 setter（生产路径由 save_error 驱动），直接改库构造状态
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (broken,))
    conn.execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                 (2 ** 31 - 1, cooling))
    conn.commit()
    conn.close()
    with TestClient(app) as http:
        counts = http.get("/healthz").json()["credentials"]
    # 4 条 trae + 种子的一条 zen、一条 kilo（都 ready）
    assert counts == {"total": 6, "ready": 3, "cooling": 1, "paused": 1,
                      "disabled": 1}
