"""The coding-session in-process MCP surface (session tools spec §3)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccswitch.engines import AgentEngine

CASES = Path("docs/superpowers/evals/session-tools/cases.jsonl")


def _ctx(record=None, session_id="s1"):
    from server.sessions.tools.context import SessionToolContext
    rec = record or SimpleNamespace(
        id=session_id, name="Fix #12", repo="o/n", branch="ccswitch/x", cwd="/tmp/w",
        origin="backlog-runner", origin_ref="o/n#12", engine=AgentEngine.CLAUDE_CODE,
        model="sonnet", permission_mode="acceptEdits", account="alpha", host="dobby",
        active_goal=None, inferred_goal="Fix the flaky test", kind=None)
    return SessionToolContext(session_id=session_id, identity="u@x", kind=None,
                              engine="claude_code", record=lambda: rec, host="dobby")


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_registry_is_a_registered_surface_with_two_read_tools():
    """The two READ tools are unconditional on every session, marker or tool contract
    alike — the three SIGNAL tools (mark_complete, mark_no_pr, request_model) are also
    registered here (server/tests/test_session_signal_tools.py), but only mounted per
    engine capability, so this asserts presence rather than an exact set."""
    from server import mcp_spec
    from server.sessions.tools.registry import SESSION_TOOL_SPECS
    read_tools = {"session_context", "report_feedback"}
    assert read_tools <= set(SESSION_TOOL_SPECS)
    assert all(SESSION_TOOL_SPECS[name].tool_class is mcp_spec.ToolClass.READ
               for name in read_tools)
    assert mcp_spec.surfaces()["session"] is SESSION_TOOL_SPECS
    assert mcp_spec.annotation_for(
        mcp_spec.MCP_PREFIX + "report_feedback").tool_class is mcp_spec.ToolClass.READ


def test_every_session_tool_has_a_spec_and_an_eval_case():
    """The default context here mounts no signalling tools (`signalling_tools` and
    `self_model_switch` default False), so `names` still covers exactly the two READ
    tools every session gets regardless of engine. Eval coverage, however, is checked
    against the FULL registry (all five specs, including the three SIGNAL tools
    mounted only per engine capability) — restored to that exact-set requirement now
    that mark_complete/mark_no_pr/request_model have their own eval cases in
    docs/superpowers/evals/session-tools/cases.jsonl. This is what stops a tool
    shipping without an eval case."""
    from server.sessions.tools.registry import SESSION_TOOL_SPECS
    from server.sessions.tools.tools import build_session_tools
    tools = build_session_tools(_ctx())
    names = {t.name for t in tools}
    assert names == {"session_context", "report_feedback"}
    assert names <= set(SESSION_TOOL_SPECS)
    covered = set()
    for line in CASES.read_text().splitlines():
        if line.strip():
            covered.update(json.loads(line)["expect_tools"])
    assert set(SESSION_TOOL_SPECS) <= covered


@pytest.mark.asyncio
async def test_session_context_returns_the_record_snapshot():
    from server.sessions.tools.tools import build_session_tools
    out = await _tool(build_session_tools(_ctx()), "session_context").handler({})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"]["repo"] == "o/n" and payload["data"]["originRef"] == "o/n#12"
    assert payload["data"]["goal"] == "Fix the flaky test" and payload["data"]["host"] == "dobby"
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_report_feedback_records_and_audits(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, SessionFeedback
    from server.sessions.tools.tools import build_session_tools
    tool = _tool(build_session_tools(_ctx()), "report_feedback")
    out = await tool.handler({"category": "dispatch", "impact": "degraded",
                              "title": "Branch missing", "body": "The prompt named feat/x."})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"]["status"] == "pending"
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.session_id == "s1" and row.context["originRef"] == "o/n#12"
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert {"session.feedback", "session.tool_call"} <= types


@pytest.mark.asyncio
async def test_bad_args_are_a_tool_error_not_an_exception(isolated_db):
    from server.sessions.tools.tools import build_session_tools
    tool = _tool(build_session_tools(_ctx()), "report_feedback")
    out = await tool.handler({"category": "bug", "impact": "minor", "title": "x", "body": "y"})
    assert out.get("is_error") is True and "category" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_handler_exception_never_escapes(isolated_db, monkeypatch):
    from server.sessions.tools import tools as mod
    async def boom(**kw):
        raise RuntimeError("db gone")
    monkeypatch.setattr(mod, "record_feedback", boom)
    tool = _tool(mod.build_session_tools(_ctx()), "report_feedback")
    out = await tool.handler({"category": "prompt", "impact": "minor", "title": "Long enough", "body": "b" * 10})
    assert out.get("is_error") is True


def test_server_is_named_from_the_brand():
    from ccswitch import brand
    from server.sessions.tools.server import build_session_server
    cfg = build_session_server(_ctx())
    assert cfg["type"] == "sdk" and cfg["name"] == brand.MCP_SERVER_NAME


def test_instruction_is_an_index_and_the_tool_description_carries_the_rules():
    """The system-prompt paragraph is deliberately an index (task-8 review): it names
    the tool, but the RULES (never a bug in the project, keep working) live only in
    the tool's own description, which rides the tool schema whenever it is mounted —
    a second copy in the paragraph is the parallel-implementation anti-pattern this
    module's docstring warns about, and it was the WEAKER copy."""
    from server.sessions.tools.instruction import session_instructions
    from server.sessions.tools.tools import build_session_tools
    s = session_instructions(session_tools=True, signalling_tools=False,
                             self_model_switch=False)
    assert "report_feedback" in s and "session_context" in s

    desc = {t.name: t.description for t in build_session_tools(_ctx())}
    assert "never a bug in the project" in desc["report_feedback"]
    assert "keep working" in desc["report_feedback"].lower()
