"""Test that _record_to_dict exposes origin and originRef on the wire."""
from __future__ import annotations

from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _make_record(**kwargs):
    defaults = dict(
        id="s1",
        name="test",
        account="acct-a",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="cs1",
        status="idle",
        created_at=None,
        detached=False,
        cols=80,
        rows=24,
        cwd="/tmp",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_record_to_dict_exposes_origin():
    rec = _make_record(origin="pr-doctor")
    d = _record_to_dict(rec)
    assert d["origin"] == "pr-doctor"


def test_record_to_dict_origin_none_when_not_set():
    rec = _make_record()
    d = _record_to_dict(rec)
    assert d["origin"] is None


def test_record_to_dict_exposes_origin_ref():
    rec = _make_record(origin="pr-doctor", origin_ref="owner/repo#42")
    d = _record_to_dict(rec)
    assert d["originRef"] == "owner/repo#42"


def test_record_to_dict_origin_ref_none_when_not_set():
    rec = _make_record()
    d = _record_to_dict(rec)
    assert d["originRef"] is None
