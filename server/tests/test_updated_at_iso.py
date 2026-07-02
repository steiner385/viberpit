from datetime import datetime
from server.sessions.types import SessionRecord


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", claude_session_id=None,
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_updated_at_iso_prefers_last_turn_at():
    r = _rec(created_at=datetime(2020, 1, 1), last_turn_at=datetime(2021, 6, 1))
    assert r.updated_at_iso == "2021-06-01T00:00:00+00:00"


def test_updated_at_iso_falls_back_to_created_at():
    r = _rec(created_at=datetime(2020, 1, 1), last_turn_at=None)
    assert r.updated_at_iso == "2020-01-01T00:00:00+00:00"


def test_updated_at_iso_none_when_no_timestamps():
    r = _rec(created_at=None, last_turn_at=None)
    assert r.updated_at_iso is None


def test_to_dict_uses_updated_at_iso():
    r = _rec(created_at=datetime(2020, 1, 1), last_turn_at=datetime(2021, 6, 1))
    assert r.to_dict()["updatedAt"] == r.updated_at_iso
