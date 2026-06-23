# server/tests/test_session_branch_persist.py
from server.sessions.types import SessionRecord


def test_session_record_roundtrips_branch():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )
    assert rec.branch == "ccswitch/main-abcd"
    assert rec.to_dict()["branch"] == "ccswitch/main-abcd"
    assert SessionRecord.from_dict(rec.to_dict()).branch == "ccswitch/main-abcd"


def test_session_record_branch_defaults_none():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt",
    )
    assert rec.branch is None
    assert rec.to_dict()["branch"] is None
