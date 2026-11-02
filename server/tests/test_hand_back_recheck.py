"""`hand_back(recheck_at=…)`: a time-gated hand-back records when to look again (#3216)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from server.mcp_spec import ToolArgError
from server.sessions.completion import parse_recheck_at
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.tools import build_session_tools

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def test_parse_accepts_a_near_future_time_and_normalises_to_utc():
    assert parse_recheck_at({"recheck_at": "2026-11-03T08:00:00-04:00"}, now=NOW) == \
        "2026-11-03T12:00:00+00:00"
    assert parse_recheck_at({}, now=NOW) is None


@pytest.mark.parametrize("bad", ["tomorrow", "2026-11-01T00:00:00Z",
                                 (NOW + timedelta(days=31)).isoformat()])
def test_parse_rejects_malformed_past_or_too_far(bad):
    with pytest.raises(ToolArgError):
        parse_recheck_at({"recheck_at": bad}, now=NOW)


def test_handler_records_the_recheck_through_the_bound_seam():
    seen = []

    async def note(iso):
        seen.append(iso)
    ctx = SessionToolContext(session_id="s", identity="u", kind="coding", engine="claude_code",
                             record=lambda: None, signalling_tools=True, note_recheck=note)
    tool = next(t for t in build_session_tools(ctx) if t.name == "hand_back")
    at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    asyncio.run(tool.handler({"reason": "gate unmet", "recheck_at": at}))
    assert len(seen) == 1
    assert asyncio.run(tool.handler({"reason": "x", "recheck_at": "nope"})).get("is_error")
    assert len(seen) == 1
