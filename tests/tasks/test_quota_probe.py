"""额度探测任务。"""

from __future__ import annotations

import pytest

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.provider.base import Quota
from src.provider.codebuddy.checkin import CheckinResult
from src.provider.codebuddy.client import CodeBuddyCredential
from src.tasks.pacer import Pacer
from src.tasks.quota_probe import QuotaProbeTask
from tests.helpers.constants import SECRET


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()

class ProbeProvider:
    id = "codebuddy"

    def __init__(self, quota=None, error=None, checkin_ok=True, refresh_new=None):
        self.quota = quota or Quota(remaining=8, total=10, probed_at=1)
        self.error = error
        self.checkin_ok = checkin_ok
        self.refresh_new = refresh_new
        self.checkin_calls = 0
        self.refresh_calls = 0
        self.seen_scopes: list[str] = []

    async def probe_quota(self, _data):
        if self.error is not None:
            raise self.error
        return self.quota

    async def checkin(self, _data):
        self.checkin_calls += 1
        return CheckinResult(ok=self.checkin_ok, credit=10 if self.checkin_ok else None)

    def checkin_scope(self, data):
        scope = f"acct:{data.get('account_uid', '')}"
        self.seen_scopes.append(scope)
        return scope

    def credential_from(self, data):
        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):
        self.refresh_calls += 1
        return {**data, "bearer_token": self.refresh_new or "NEW"}

async def test_quota_probe_updates_health(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    report = await task.run_once()
    assert report.succeeded == 1
    assert credentials.candidates()[0].health == 80

async def test_quota_probe_failure_marks_unknown_not_zero(repo):
    """探测失败 → health=NULL（unknown），绝不当作 0 分（Q26 三态）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider(error=RuntimeError("boom"))})
    report = await task.run_once()
    assert report.failed == 1
    assert credentials.candidates()[0].health is None

async def test_quota_probe_skips_unknown_provider_and_missing_data(repo):
    credentials, _db = repo
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = QuotaProbeTask(credentials, {})
    report = await task.run_once()
    assert report.skipped == 1

async def test_quota_probe_uses_pacer(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    turns: list[int] = []

    class CountingPacer(Pacer):
        async def wait_turn(self) -> None:
            turns.append(1)

    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()},
                          CountingPacer(1, 1))
    await task.run_once()
    assert len(turns) == 1
    turns.clear()
    await task.run_once(apply_pacing=False)
    assert turns == []

async def test_quota_probe_skips_when_credential_vanished(repo):
    """凭证在探测前被删除 → 计入 skipped（background 93-96）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    original = credentials.credential_data
    credentials.credential_data = lambda _cid: None       # type: ignore[method-assign]
    report = await task.run_once()
    credentials.credential_data = original                # type: ignore[method-assign]
    assert report.skipped == 1
