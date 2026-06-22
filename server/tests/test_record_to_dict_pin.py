"""Test that _record_to_dict exposes accountPinned and model on the wire."""
from __future__ import annotations

from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _make_record(**kwargs) -> SessionRecord:
    defaults = dict(
        id="s1",
        name="test",
        account="acct-a",
        claude_session_id="cs1",
        status="idle",
        created_at=None,
        detached=False,
        cols=80,
        rows=24,
        cwd="/tmp",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_record_to_dict_exposes_account_pinned():
    rec = _make_record(account_pinned=True)
    d = _record_to_dict(rec)
    assert d["accountPinned"] is True


def test_record_to_dict_account_pinned_defaults_false():
    rec = _make_record()
    d = _record_to_dict(rec)
    assert d["accountPinned"] is False


def test_record_to_dict_exposes_model():
    rec = _make_record(model="opus")
    d = _record_to_dict(rec)
    assert d["model"] == "opus"


def test_record_to_dict_model_none_when_not_set():
    rec = _make_record()
    d = _record_to_dict(rec)
    assert d["model"] is None
