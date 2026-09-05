# server/tests/test_rev_at_token.py
from datetime import datetime
from typing import Any
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**kw: Any) -> SessionRecord:
    base: dict[str, Any] = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id=None,
                                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_rev_at_is_updated_at_iso():
    r = _rec(updated_at=datetime(2021, 6, 1, 12, 0, 0))
    assert r.rev_at == "2021-06-01T12:00:00+00:00"


def test_rev_at_none_when_no_updated_at():
    assert _rec(updated_at=None).rev_at is None


def test_to_dict_emits_revAt():
    r = _rec(updated_at=datetime(2021, 6, 1, 12, 0, 0))
    assert r.to_dict()["revAt"] == r.rev_at


def test_from_dict_restores_updated_at_via_revAt():
    from datetime import timezone
    r = _rec(updated_at=datetime(2021, 6, 1, 12, 0, 0),
             created_at=datetime(2020, 1, 1))
    back = SessionRecord.from_dict(r.to_dict())
    # The wire carries an explicit UTC offset; fromisoformat gives a tz-aware datetime —
    # the SAME instant as the naive UTC input (same pattern as last_turn_at round-trip).
    assert back.updated_at == r.updated_at.replace(tzinfo=timezone.utc)
    assert back.rev_at == r.rev_at
