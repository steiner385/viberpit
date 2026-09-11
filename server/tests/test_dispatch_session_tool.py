"""The dispatch_session tool (child-session spec §4.2, §4.5)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import mcp_spec
from server.sessions.tools.context import SessionToolContext, context_snapshot
from server.sessions.tools.registry import SESSION_TOOL_SPECS, mounted_names
from server.sessions.tools.tools import build_session_tools


def _rec(**over):
    base = dict(id="p1", name="Parent", repo="o/n", branch="b", cwd="/tmp/w", origin=None,
                origin_ref=None, engine="claude_code", model="sonnet", permission_mode="auto",
                account="alpha", host="dobby", active_goal=None, inferred_goal=None, kind=None,
                parent_session_id=None)
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(*, child_sessions=True, dispatch=None, children=None, record=None):
    rec = record or _rec()
    return SessionToolContext(session_id="p1", identity="u@x", kind=None, engine="claude_code",
                              record=lambda: rec, host="dobby", signalling_tools=True,
                              child_sessions=child_sessions, dispatch_child=dispatch,
                              children=children or (lambda: []))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _payload(result):
    return json.loads(result["content"][0]["text"])


def test_spec_is_a_mutation_and_mount_is_gated():
    assert SESSION_TOOL_SPECS["dispatch_session"].tool_class is mcp_spec.ToolClass.MUTATION
    assert "dispatch_session" in mounted_names(signalling_tools=True, self_model_switch=False, child_sessions=True)
    assert "dispatch_session" not in mounted_names(signalling_tools=True, self_model_switch=False, child_sessions=False)
    assert "dispatch_session" not in mounted_names(signalling_tools=False, self_model_switch=False, child_sessions=True)
    assert "dispatch_session" not in {t.name for t in build_session_tools(_ctx(child_sessions=False))}
    assert "dispatch_session" in {t.name for t in build_session_tools(_ctx())}


@pytest.mark.asyncio
async def test_handler_calls_dispatch_child_once_with_its_args():
    dispatch = AsyncMock(return_value={"id": "c1", "name": "Child", "url": "/sessions/c1", "status": "idle"})
    tools = build_session_tools(_ctx(dispatch=dispatch))
    out = await _tool(tools, "dispatch_session").handler(
        {"name": "Child", "prompt": "Do X", "model": "sonnet", "permission_mode": "interactive"})
    dispatch.assert_awaited_once_with(name="Child", prompt="Do X", model="sonnet",
                                      permission_mode="interactive")
    assert _payload(out)["data"]["id"] == "c1"


@pytest.mark.asyncio
async def test_handler_rejects_bad_args_before_dispatch():
    dispatch = AsyncMock()
    tools = build_session_tools(_ctx(dispatch=dispatch))
    t = _tool(tools, "dispatch_session")
    for bad in ({"name": "ab", "prompt": "x"}, {"name": "ok name", "prompt": "  "},
                {"name": "ok name", "prompt": "x", "model": "gpt-9"},
                {"name": "ok name", "prompt": "x", "permission_mode": "root"}):
        out = await t.handler(bad)
        assert out.get("is_error") is True, bad
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_limit_refusal_is_reported_as_the_tool_result():
    from server.sessions.child_sessions import ChildSessionLimit
    dispatch = AsyncMock(side_effect=ChildSessionLimit("this session already has 3 live children (cap 3)"))
    tools = build_session_tools(_ctx(dispatch=dispatch))
    out = await _tool(tools, "dispatch_session").handler({"name": "Child", "prompt": "x"})
    assert out.get("is_error") is True and "cap 3" in out["content"][0]["text"]


def test_session_context_lists_children_and_parent():
    kids = [SimpleNamespace(id="c1", name="Child", status="idle")]
    snap = context_snapshot(_ctx(children=lambda: kids, record=_rec(parent_session_id="root")))
    assert snap["parentSessionId"] == "root"
    assert snap["children"] == [{"id": "c1", "name": "Child", "status": "idle"}]


def test_instruction_names_the_tool_only_when_mounted():
    from server.sessions.tools.instruction import session_instructions
    on = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False, child_sessions=True)
    off = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False, child_sessions=False)
    assert "dispatch_session" in on and "dispatch_session" not in off
