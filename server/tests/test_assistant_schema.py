"""Schema + record additions for assistant-kind sessions (spec §5)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from server.audit import record_audit
from server.db.models import AppSettings, AuditEvent, Session
from server.sessions.engine import AgentEngine
from server.sessions.types import ASSISTANT_KIND, SessionRecord, is_assistant


def test_session_record_kind_defaults_to_coding():
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="", cols=80, rows=24, cwd="/tmp")
    assert rec.kind is None
    assert rec.owner_identity is None
    assert rec.last_activity_at is None
    assert is_assistant(rec) is False


def test_is_assistant_true_for_kind():
    rec = SessionRecord(id="s1", name="Assistant", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="", cols=80,
                        rows=24, cwd="/tmp", kind=ASSISTANT_KIND, owner_identity="u@example.com")
    assert is_assistant(rec) is True


def test_db_columns_exist():
    cols = {c.name for c in Session.__table__.columns}
    assert {"kind", "owner_identity"} <= cols
    settings_cols = {c.name for c in AppSettings.__table__.columns}
    assert {"assistant_enabled", "assistant_model", "assistant_operator_tools"} <= settings_cols
    # Nullable is what lets init_db._reconcile_columns ALTER-ADD it on both dialects; a
    # NOT NULL column here would need a real migration and would fail the drift assert.
    assert AppSettings.__table__.columns["assistant_operator_tools"].nullable is True
    assert "via" in {c.name for c in AuditEvent.__table__.columns}
    for name in ("kind", "owner_identity"):
        assert Session.__table__.columns[name].nullable is True


@pytest.mark.asyncio
async def test_record_audit_via(isolated_db):
    # `isolated_db` points CCSWITCH_DB_URL at a fresh per-test file and inits it;
    # sessions come from the normal `db_session()` factory.
    from server.db import db_session
    async with db_session() as db:
        ev = await record_audit(db, "assistant.tool_call", "u@example.com",
                                {"tool": "list_sessions"}, via="assistant")
        await db.commit()
        row = (await db.execute(select(AuditEvent).where(AuditEvent.id == ev.id))).scalar_one()
        assert row.via == "assistant"
        default = await record_audit(db, "x", "u@example.com", {})
        assert default.via is None
