# server/tests/test_rehydrate_persists_model_pin.py
"""Regression tests: model and account_pinned are read back from the DB row
on both rehydrate and unarchive (Critical #1 fix)."""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _make_row(**kwargs):
    """Build a minimal fake DB row for testing row→SessionRecord mapping."""
    defaults = dict(
        id="s1",
        name="test",
        account_name="acct-a",
        claude_session_id="cid1",
        cwd="/tmp",
        repo=None,
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
    """Minimal async context-manager DB that returns rows synchronously."""

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

    def first(self):
        return self._rows[0] if self._rows else None


def _make_orch_with_records(existing_ids=None):
    """Create a bare SessionOrchestrator with an empty _records dict."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    if existing_ids:
        for sid in existing_ids:
            orch._records[sid] = object()  # sentinel: already present
    return orch


# ── Rehydrate ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rehydrate_restores_model(monkeypatch):
    row = _make_row(model="claude-opus-4-5", account_pinned=False)
    orch = _make_orch_with_records()

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    added = await orch.rehydrate()
    assert added == 1
    rec = orch._records["s1"]
    assert rec.model == "claude-opus-4-5"


@pytest.mark.asyncio
async def test_rehydrate_restores_account_pinned(monkeypatch):
    row = _make_row(model=None, account_pinned=True)
    orch = _make_orch_with_records()

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    added = await orch.rehydrate()
    assert added == 1
    rec = orch._records["s1"]
    assert rec.account_pinned is True


@pytest.mark.asyncio
async def test_rehydrate_both_fields_together(monkeypatch):
    row = _make_row(model="opus", account_pinned=True)
    orch = _make_orch_with_records()

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    await orch.rehydrate()
    rec = orch._records["s1"]
    assert rec.model == "opus"
    assert rec.account_pinned is True


@pytest.mark.asyncio
async def test_rehydrate_defaults_when_columns_absent(monkeypatch):
    """getattr fallback: row without model/account_pinned attrs gets defaults."""
    row = SimpleNamespace(
        id="s1", name="test", account_name="acct-a", claude_session_id="cid1",
        cwd="/tmp", repo=None, status="idle", name_is_auto=True,
        permission_mode="interactive", created_at=datetime(2025, 1, 1),
        # deliberately omit model and account_pinned
    )
    orch = _make_orch_with_records()

    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    await orch.rehydrate()
    rec = orch._records["s1"]
    assert rec.model is None
    assert rec.account_pinned is False
