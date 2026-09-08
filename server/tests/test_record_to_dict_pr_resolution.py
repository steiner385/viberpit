"""The browser serializer must carry a session's PR resolution to the wire.

`SessionRecord.to_dict` (the daemon→edge RPC copy) has emitted `prResolution`
since the marker landed, but `_record_to_dict` (the browser copy) never did — so a
queued session ended as "obsolete: PR #N merged before start" reached the
dashboard as a bare "Ended" over an empty conversation, with the reason sitting
unread in the DB row.
"""
from __future__ import annotations

from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _make_record(**kwargs):
    defaults = dict(
        id="s1", name="test", account="acct-a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="", status="ended", created_at=None, detached=True,
        cols=80, rows=24, cwd="/tmp",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_record_to_dict_exposes_pr_resolution_and_source():
    rec = _make_record(
        pr_resolution="obsolete: PR #15776 merged before start",
        pr_resolution_source="queue-drain",
    )
    d = _record_to_dict(rec)
    assert d["prResolution"] == "obsolete: PR #15776 merged before start"
    assert d["prResolutionSource"] == "queue-drain"


def test_record_to_dict_pr_resolution_null_when_unset():
    d = _record_to_dict(_make_record())
    assert d["prResolution"] is None
    assert d["prResolutionSource"] is None
