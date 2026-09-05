from server.db.models import Session as DbSession
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def test_record_defaults_name_is_auto_true():
    r = SessionRecord(id="i", name="New Session", account="a",
                      engine=AgentEngine.CLAUDE_CODE,
                      engine_session_id="c", cols=80, rows=24, cwd="/tmp")
    assert r.name_is_auto is True


def test_db_model_has_name_is_auto_column():
    assert "name_is_auto" in DbSession.__table__.columns
