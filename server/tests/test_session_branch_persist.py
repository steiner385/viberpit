# server/tests/test_session_branch_persist.py
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from server.sessions.types import SessionRecord


def test_session_record_roundtrips_branch():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )
    assert rec.branch == "ccswitch/main-abcd"
    assert rec.to_dict()["branch"] == "ccswitch/main-abcd"
    assert SessionRecord.from_dict(rec.to_dict()).branch == "ccswitch/main-abcd"


def test_session_record_branch_defaults_none():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt",
    )
    assert rec.branch is None
    assert rec.to_dict()["branch"] is None


# ── Rehydrate regression ───────────────────────────────────────────────────────


def _make_row(**kwargs):
    """Build a minimal fake DB row for testing row→SessionRecord mapping."""
    defaults = dict(
        id="s1",
        name="test",
        account_name="acct-a",
        claude_session_id="cid1",
        cwd="/tmp",
        repo="acme/widget",
        branch=None,
        status="idle",
        name_is_auto=True,
        permission_mode="interactive",
        created_at=datetime(2025, 1, 1),
        adopted_in_place=False,
        model=None,
        account_pinned=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    return orch


@pytest.mark.asyncio
async def test_rehydrate_restores_branch(monkeypatch):
    """branch survives the DB → rehydrate() round-trip (regression for omitted field)."""
    row = _make_row(branch="feat/session-pr-indicators")
    orch = _make_orch()

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    added = await orch.rehydrate()
    assert added == 1
    rec = orch._records["s1"]
    assert rec.branch == "feat/session-pr-indicators"


@pytest.mark.asyncio
async def test_rehydrate_branch_defaults_none_when_column_absent(monkeypatch):
    """getattr fallback: row without branch attr (older DB) gets branch=None."""
    row = SimpleNamespace(
        id="s1", name="test", account_name="acct-a", claude_session_id="cid1",
        cwd="/tmp", repo="acme/widget", status="idle", name_is_auto=True,
        permission_mode="interactive", created_at=datetime(2025, 1, 1),
        adopted_in_place=False, model=None, account_pinned=False,
        # deliberately omit branch
    )
    orch = _make_orch()

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    await orch.rehydrate()
    rec = orch._records["s1"]
    assert rec.branch is None
