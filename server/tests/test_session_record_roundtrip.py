"""Round-trip tests for SessionRecord.to_dict / from_dict serialization."""
from typing import Any

from server.sessions.types import SessionRecord


def _make_record(**kwargs) -> SessionRecord:
    defaults: dict[str, Any] = dict(
        id="s1",
        name="n",
        account="a",
        claude_session_id="cs1",
        cols=80,
        rows=24,
        cwd="/tmp",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_pr_resolution_roundtrips_through_dict():
    rec = _make_record()
    rec.pr_resolution = "cancelled: dropped the change"
    rec.pr_resolution_source = "marker"
    d = rec.to_dict()
    assert d["prResolution"] == "cancelled: dropped the change"
    assert d["prResolutionSource"] == "marker"
    back = SessionRecord.from_dict(d)
    assert back.pr_resolution == "cancelled: dropped the change"
    assert back.pr_resolution_source == "marker"


def test_pr_resolution_defaults_none():
    rec = _make_record()
    assert rec.pr_resolution is None
    assert rec.pr_resolution_source is None
    assert SessionRecord.from_dict(rec.to_dict()).pr_resolution is None
