"""凭证仓储：积分流水记录、裁剪与查询端点。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, CreditEventRepository
from src.main import build_app
from src.provider.base import Quota
from src.tasks.retention import RetentionTask
from tests.helpers.constants import SECRET


def test_save_quota_records_net_credit_change(tmp_path):
    """余额变化才记流水，且记的是净变化，不是动作归因。

    上游签到/成长接口不打日志；这里断言 source 只有 observed/sync 两种，
    绝不出现 checkpoint/growth 这类上游从未告知的归因。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)

    # 首次探测：只建立基线（sync），不算积分
    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now))
    first = events.recent(cid)
    assert len(first) == 1
    assert first[0]["source"] == "sync" and first[0]["delta"] is None
    assert first[0]["after"] == 100 and first[0]["before"] is None

    # 余额没变 → 不记（否则每轮探测落一行 0，表被噪声淹没）
    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now + 60))
    assert len(events.recent(cid)) == 1
    # 浮点噪声不算变化：上游 JSON 的浮点运算会给出 100.00000000000001 这种值
    credentials.save_quota(cid, Quota(remaining=100 + 1e-13, total=200, probed_at=now + 90))
    assert len(events.recent(cid)) == 1

    # 余额增加（签到/成长/任何来源都可能）→ 记净变化 + 区间起点
    credentials.save_quota(cid, Quota(remaining=115, total=200, probed_at=now + 120))
    latest = events.recent(cid)[0]
    assert latest["delta"] == 15              # 落库前已按 epsilon 收敛，不残留 14.999999
    assert latest["before"] == pytest.approx(100) and latest["after"] == 115
    # 区间起点是「上次探测」，不是「上次有变动」：中间那两次没记行但都观测过，
    # 覆盖区间必须从最近一次观测算起，否则会把一段无人观测的时间也算进去
    assert latest["window_start"] == now + 90
    assert latest["source"] == "observed"

    # 余额减少（对话消耗）→ delta 为负，同样如实记
    credentials.save_quota(cid, Quota(remaining=110, total=200, probed_at=now + 180))
    assert events.recent(cid)[0]["delta"] == -5
    db.close()

def test_save_quota_records_unquantifiable_change(tmp_path):
    """余额变未知（探测失败后）仍记行：这是异常，但绝不量化成 0。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)

    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now))
    credentials.mark_probe_failed(cid, now=now + 60)
    credentials.save_quota(cid, Quota(remaining=None, total=None, probed_at=now + 120))
    latest = events.recent(cid)[0]
    assert latest["delta"] is None            # 不猜 0
    assert latest["before"] == 100 and latest["after"] is None
    assert latest["source"] == "observed"
    db.close()

def test_save_quota_skips_event_for_unknown_to_unknown(tmp_path):
    """两端都未知 = 没学到任何东西，不写空行。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=None, total=None, probed_at=now))
    assert events.recent(cid) == []
    db.close()

def test_save_quota_event_skipped_when_credential_gone(tmp_path):
    """凭证被并发删除：不补记悬挂行（查询返回 None 的分支）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    credentials.save_quota("cred_missing", Quota(remaining=1, total=2, probed_at=1))
    assert events.recent("cred_missing") == []
    db.close()

def test_credit_events_recent_is_bounded_and_ordered(tmp_path):
    """倒序返回，limit 收敛到 [1, 200]（防止一次拉爆前端）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=100)
    for step in range(5):
        credentials.save_quota(cid, Quota(remaining=100 - step, total=200, probed_at=100 + step))
    rows = events.recent(cid)
    assert [row["ts"] for row in rows] == [104, 103, 102, 101, 100]  # 最新在前
    assert len(events.recent(cid, limit=2)) == 2
    assert len(events.recent(cid, limit=0)) == 1        # 下限收敛
    assert len(events.recent(cid, limit=9999)) == 5     # 上限不报错
    # 别的凭证的记录不会串进来
    other = credentials.add(provider="trae", credential_data={"accessToken": "t"}, now=100)
    assert events.recent(other) == []
    db.close()

def test_credit_events_prune_drops_old_rows(tmp_path):
    """保留期清理：只删过期行，保留期内不动。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=1, total=2, probed_at=now - 100 * 86400))
    credentials.save_quota(cid, Quota(remaining=2, total=2, probed_at=now))
    assert events.prune(keep_days=90, now=now) == 1
    assert [row["after"] for row in events.recent(cid)] == [2]
    db.close()

def test_credited_events_endpoint_requires_admin_and_known_credential(tmp_path):
    """端到端：非管理员 403；未知凭证 400；已知凭证返回流水。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        now = int(time.time())
        repo = app.state.credentials
        cid = repo.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
        repo.save_quota(cid, Quota(remaining=50, total=100, probed_at=now))
        repo.save_quota(cid, Quota(remaining=60, total=100, probed_at=now + 60))

        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        body = client.get(f"/api/credentials/{cid}/credit-events").json()
        assert [event["delta"] for event in body["events"]] == [10, None]
        assert body["events"][1]["source"] == "sync"
        # limit 透传
        assert len(client.get(
            f"/api/credentials/{cid}/credit-events?limit=1").json()["events"]) == 1
        # 未知凭证 → 400（不是空列表，避免拼错 id 时看起来「没有记录」）
        assert client.get("/api/credentials/cred_nope/credit-events").status_code == 400
        # 非管理员（conftest 里存在的 alice）→ 403
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        assert client.get(f"/api/credentials/{cid}/credit-events").status_code == 403

def test_retention_task_prunes_credit_events(tmp_path):
    """保留任务顺带回收积分流水；未传 credit_events 时该键为 0（老调用方兼容）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=1, total=2, probed_at=now - 100 * 86400))
    credentials.save_quota(cid, Quota(remaining=2, total=2, probed_at=now))

    class FakeCollector:
        def rollup_hourly(self) -> int:
            return 3

        def purge_expired(self, days: int) -> int:
            return 7

    task = RetentionTask(FakeCollector(), retention_days=90,
                         credentials=credentials, credit_events=events)
    report = task.run_once()
    assert report["rolled_up"] == 3 and report["purged"] == 7
    assert report["purged_credit_events"] == 1
    # 老调用方（不传 credit_events）仍然可用
    bare = RetentionTask(FakeCollector(), retention_days=90, credentials=credentials)
    assert bare.run_once()["purged_credit_events"] == 0
    db.close()
