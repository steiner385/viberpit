"""`rename_self`: a session retitles itself through the one rename seam (#3214)."""
import asyncio
import json

from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.registry import TURN_SIGNAL_NAMES, mounted_names
from server.sessions.tools.tools import build_session_tools


def _call(rename, args):
    ctx = SessionToolContext(session_id="s", identity="u", kind="coding", engine="claude_code",
                             record=lambda: None, signalling_tools=True, rename=rename)
    tool = next(t for t in build_session_tools(ctx) if t.name == "rename_self")
    return asyncio.run(tool.handler(args))


def test_renames_through_the_bound_seam_and_collapses_whitespace():
    seen = []

    async def rename(name):
        seen.append(name)
    out = _call(rename, {"name": "  KinDash   CI monitor "})
    assert seen == ["KinDash CI monitor"]
    assert json.loads(out["content"][0]["text"])["data"] == {"name": "KinDash CI monitor"}


def test_rejects_out_of_range_names_without_renaming():
    seen = []

    async def rename(name):
        seen.append(name)
    for bad in ("", "ab", "x" * 61, None):
        assert _call(rename, {"name": bad}).get("is_error")
    assert seen == []


def test_unwired_context_is_an_error_not_a_crash():
    assert _call(None, {"name": "valid name"}).get("is_error")


def test_mounted_only_with_signalling_tools_and_not_a_turn_signal():
    assert "rename_self" in mounted_names(signalling_tools=True, self_model_switch=False)
    assert "rename_self" not in mounted_names(signalling_tools=False, self_model_switch=False)
    assert "rename_self" not in TURN_SIGNAL_NAMES
