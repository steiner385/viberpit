"""Test that _record_to_dict exposes accountPinned and model on the wire."""
from __future__ import annotations

import datetime as _dt

from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _make_record(**kwargs):
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


def test_record_to_dict_serializes_updated_at():
    """The 'last active' timestamp must reach the wire (regression: it was
    hardcoded to None, so the UI parsed null -> epoch 0 -> 'active 20627d ago')."""
    ts = _dt.datetime(2026, 6, 22, 11, 58, 0)
    rec = _make_record(updated_at=ts)
    d = _record_to_dict(rec)
    assert d["updatedAt"] == ts.isoformat()


def test_record_to_dict_updated_at_falls_back_to_created_at():
    """A record with no activity yet (no updated_at) should report its creation
    time rather than nothing — a freshly created session is 'active' just now."""
    created = _dt.datetime(2026, 6, 22, 11, 0, 0)
    rec = _make_record(created_at=created, updated_at=None)
    d = _record_to_dict(rec)
    assert d["updatedAt"] == created.isoformat()


def test_record_to_dict_updated_at_none_when_no_timestamps():
    """With neither timestamp, emit null so the client omits the label rather
    than rendering an epoch-0 sentinel."""
    rec = _make_record(created_at=None, updated_at=None)
    d = _record_to_dict(rec)
    assert d["updatedAt"] is None
