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


def test_record_to_dict_label_uses_last_turn_at():
    """The wire 'updatedAt' (the "active … ago" label) is the most recent turn's
    response time — NOT record.updated_at, which any row write bumps (the bug that
    pinned every card to '0s ago'). It must reach the wire (regression: a null here
    parsed to epoch 0 → 'active 20627d ago')."""
    turn = _dt.datetime(2026, 6, 22, 11, 58, 0)
    # updated_at is intentionally more recent — the label must ignore it.
    rec = _make_record(last_turn_at=turn, updated_at=_dt.datetime(2026, 6, 22, 12, 0, 0))
    d = _record_to_dict(rec)
    assert d["updatedAt"] == turn.isoformat()


def test_record_to_dict_label_falls_back_to_created_at():
    """A session with no turn yet (last_turn_at None) reports its creation time
    rather than nothing — a freshly created session is 'active' just now."""
    created = _dt.datetime(2026, 6, 22, 11, 0, 0)
    rec = _make_record(created_at=created, last_turn_at=None)
    d = _record_to_dict(rec)
    assert d["updatedAt"] == created.isoformat()


def test_record_to_dict_label_none_when_no_timestamps():
    """With neither a turn nor a creation time, emit null so the client omits the
    label rather than rendering an epoch-0 sentinel."""
    rec = _make_record(created_at=None, last_turn_at=None)
    d = _record_to_dict(rec)
    assert d["updatedAt"] is None
