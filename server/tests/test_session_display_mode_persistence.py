"""display_mode survives every hop: DB row → SessionRecord → RPC dict → API payload."""
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="", cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_record_defaults_to_none():
    assert _rec().display_mode is None


def test_to_dict_from_dict_roundtrip():
    d = _rec(display_mode="native").to_dict()
    assert d["displayMode"] == "native"
    assert SessionRecord.from_dict(d).display_mode == "native"
    assert SessionRecord.from_dict({k: v for k, v in d.items() if k != "displayMode"}).display_mode is None


def test_db_model_has_nullable_column():
    from server.db.models import Session
    col = Session.__table__.c.display_mode
    assert col.nullable is True


def test_record_from_row_maps_display_mode():
    from types import SimpleNamespace
    from server.sessions.orchestrator import SessionOrchestrator
    row = SimpleNamespace(id="s1", name="n", account_name="a", engine=AgentEngine.CLAUDE_CODE.value,
                          engine_session_id="", cwd="/tmp", repo=None, status="idle",
                          name_is_auto=True, permission_mode="interactive", created_at=None,
                          display_mode="native")
    rec = SessionOrchestrator._record_from_row(SessionOrchestrator.__new__(SessionOrchestrator), row)
    assert rec.display_mode == "native"


def test_api_serializer_emits_display_mode():
    from server.api.sessions import _record_to_dict
    payload = _record_to_dict(_rec(display_mode="virtual"))
    assert payload["displayMode"] == "virtual"


def test_migration_chain():
    import importlib
    m = importlib.import_module("server.db.migrations.versions.0030_session_display_mode")
    assert m.revision == "0030_session_display_mode"
    assert m.down_revision == "0029_notification_rule_scope_session"
