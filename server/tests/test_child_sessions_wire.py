"""parent_session_id rides both serializers and the DB row (child-session spec §4.1)."""
import types

from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**over):
    base = dict(
        id="c1", name="child", account="a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None, reasoning_effort=None, repo=None, status="idle",
        name_is_auto=False, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False, updated_at_iso=None, rev_at=None, cwd=None,
        parent_session_id="p1",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_http_serializer_emits_parent_session_id():
    assert _record_to_dict(_rec())["parentSessionId"] == "p1"
    assert _record_to_dict(_rec(parent_session_id=None))["parentSessionId"] is None


def test_record_roundtrips_parent_fields_over_rpc():
    rec = SessionRecord(id="c1", name="child", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x",
                        parent_session_id="p1", parent_notified_status="done")
    d = rec.to_dict()
    assert d["parentSessionId"] == "p1" and d["parentNotifiedStatus"] == "done"
    back = SessionRecord.from_dict(d)
    assert back.parent_session_id == "p1" and back.parent_notified_status == "done"


def test_a_pre_feature_rpc_payload_reads_as_a_root():
    rec = SessionRecord(id="r1", name="root", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    d = rec.to_dict()
    d.pop("parentSessionId"); d.pop("parentNotifiedStatus")
    back = SessionRecord.from_dict(d)
    assert back.parent_session_id is None and back.parent_notified_status is None


def test_db_session_has_nullable_parent_columns():
    from server.db.models import Session
    for col in ("parent_session_id", "parent_notified_status"):
        assert Session.__table__.c[col].nullable is True
