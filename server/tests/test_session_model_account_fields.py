from server.db.models import Session
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def test_db_model_has_model_and_account_pinned_columns():
    cols = {c.name for c in Session.__table__.columns}
    assert "model" in cols
    assert "account_pinned" in cols
    # Must be nullable so init_db backfills without a migration.
    assert Session.__table__.c.model.nullable is True
    assert Session.__table__.c.account_pinned.nullable is True


def test_session_record_round_trips_model_and_pin():
    rec = SessionRecord(
        id="s1", name="n", account="acct-a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
        cols=80, rows=24, cwd="/tmp", model="opus", account_pinned=True,
    )
    d = rec.to_dict()
    assert d["model"] == "opus"
    assert d["accountPinned"] is True
    back = SessionRecord.from_dict(d)
    assert back.model == "opus"
    assert back.account_pinned is True


def test_session_record_defaults_are_none_and_false():
    rec = SessionRecord(
        id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
        cols=80, rows=24, cwd="/tmp",
    )
    assert rec.model is None
    assert rec.account_pinned is False
    assert SessionRecord.from_dict(rec.to_dict()).account_pinned is False
