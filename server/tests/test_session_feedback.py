from __future__ import annotations

import pytest


def test_session_tools_flags_default(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_SESSION_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSION_FEEDBACK_CAP", raising=False)
    s = Settings()
    assert s.session_tools_enabled is True
    assert s.session_feedback_cap == 3


def test_session_tools_flags_from_env(monkeypatch):
    from server.config import Settings
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    monkeypatch.setenv("CCSWITCH_SESSION_FEEDBACK_CAP", "7")
    s = Settings()
    assert s.session_tools_enabled is False
    assert s.session_feedback_cap == 7


def test_flags_are_listed_on_about():
    from server.api.settings import _MANAGED
    keys = {row[0] for row in _MANAGED}
    assert {"session_tools_enabled", "session_feedback_cap"} <= keys


@pytest.mark.asyncio
async def test_session_feedback_table_round_trips(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import SessionFeedback
    async with db_session() as db:
        db.add(SessionFeedback(session_id="s1", fingerprint="abc", category="dispatch",
                               impact="degraded", title="t", body="b", status="pending",
                               context={"repo": "o/n"}))
        await db.commit()
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.id and row.reported_at is not None and row.issue_number is None
        assert row.context == {"repo": "o/n"}


def test_migration_0029_chains_off_the_current_head():
    import importlib
    mod = importlib.import_module("server.db.migrations.versions.0029_session_feedback")
    assert mod.revision == "0029_session_feedback"
    assert mod.down_revision == "0028_assistant_tokens"
