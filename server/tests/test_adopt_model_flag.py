"""The adopted_in_place flag on the Session model + SessionRecord."""
from __future__ import annotations

from server.db.models import Session
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def test_session_model_has_nullable_adopted_flag():
    col = Session.__table__.c.adopted_in_place
    assert col.nullable is True


def test_record_defaults_adopted_false():
    r = SessionRecord(id="i", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                      cols=80, rows=24, cwd="/x")
    assert r.adopted_in_place is False
