"""The coding-session in-process MCP surface (session tools spec §3)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from viberpit.engines import AgentEngine

CASES = Path("docs/superpowers/evals/session-tools/cases.jsonl")


def _ctx(record=None, session_id="s1", **kw):
    from server.sessions.tools.context import SessionToolContext
    rec = record or SimpleNamespace(
        id=session_id, name="Fix #12", repo="o/n", branch="ccswitch/x", cwd="/tmp/w",
        origin="backlog-runner", origin_ref="o/n#12", engine=AgentEngine.CLAUDE_CODE,
        model="sonnet", permission_mode="acceptEdits", account="alpha", host="dobby",
        active_goal=None, inferred_goal="Fix the flaky test", kind=None)
    return SessionToolContext(session_id=session_id, identity="u@x", kind=None,
                              engine="claude_code", record=lambda: rec, host="dobby", **kw)


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_registry_is_a_registered_surface_with_two_read_tools():
    """The two READ tools are unconditional on every session, marker or tool contract
    alike — the three SIGNAL tools (mark_complete, mark_no_pr, request_model) are also
    registered here (server/tests/test_session_signal_tools.py), but only mounted per
    engine capability, so this asserts presence rather than an exact set."""
    from server import mcp_spec
    from server.sessions.tools.registry import SESSION_TOOL_SPECS
    read_tools = {"session_context", "report_feedback",
                     "roadmap_needs_you", "roadmap_item"}
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
    assert names == {"session_context", "report_feedback",
                     "roadmap_needs_you", "roadmap_item"}
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
async def test_audit_survives_cancel_mid_commit(isolated_db, monkeypatch):
    """#2804: cancelling the tool task while the audit flush is in flight must not
    abandon the write — the commit is shielded and the row still lands."""
    import asyncio
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from server.db import db_session
    from server.db.models import AuditEvent
    from server.sessions.tools.registry import SESSION_TOOL_SPECS
    from server.sessions.tools.tools import _audit_tool_call

    real_commit = AsyncSession.commit
    started = asyncio.Event()

    async def slow_commit(self):
        started.set()
        await asyncio.sleep(0.2)
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", slow_commit)
    task = asyncio.create_task(
        _audit_tool_call(_ctx(), SESSION_TOOL_SPECS["session_context"], True, 1.0))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    async with db_session() as db:
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
    assert "session.tool_call" in types


@pytest.mark.asyncio
async def test_set_hold_resolves_the_session_ref_and_audits(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        db.add(WorkItem(repo="o/n", number=12, kind="issue", body="b", triage_state="classified"))
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "set_hold")
    out = await tool.handler({"kind": "evidence", "reason": "needs a repro"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"] == {"ref": "o/n#12", "holdKind": "evidence", "holdBy": "session:s1"}
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem))).scalar_one()
        assert wi.hold_kind == "evidence" and wi.hold_reason == "needs a repro"
        assert wi.hold_by == "session:s1"
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert "pm.hold.set" in types


@pytest.mark.asyncio
async def test_set_hold_clearing_audits_release(isolated_db):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.pm.holds import set_hold as pm_set_hold
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        wi = WorkItem(repo="o/n", number=12, kind="issue", body="b", triage_state="classified")
        pm_set_hold(wi, kind="evidence", reason="pre-existing", actor="session:s1",
                   now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        db.add(wi)
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "set_hold")
    out = await tool.handler({"kind": None, "reason": "found the cause"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"] == {"ref": "o/n#12", "holdKind": None, "holdBy": None}
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().all()
        assert len(rows) == 1
        # The row must carry what WAS released and the agent's own reason — both are
        # gone from `wi` by the time the handler reads it back, since apply_session_hold
        # already cleared them; apply_session_hold's returned `released` is what saves it.
        assert rows[0].detail["kind"] == "evidence"
        assert rows[0].detail["reason"] == "found the cause"
        # ONE release audit shape across every path that ends a hold: the signal
        # names which one it was, from the shared RELEASE_SIGNALS tuple.
        from server.pm.holds import RELEASE_SIGNALS
        assert rows[0].detail["signal"] == "session" and "session" in RELEASE_SIGNALS
        assert rows[0].via == "session"


@pytest.mark.asyncio
async def test_set_hold_null_kind_on_an_unheld_item_audits_nothing(isolated_db):
    """A release call on an item with no hold is a true no-op (matching the overlay
    route's `if released is not None` guard) — it must not write a `pm.hold.*` row."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        db.add(WorkItem(repo="o/n", number=12, kind="issue", body="b", triage_state="classified"))
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "set_hold")
    out = await tool.handler({"kind": None, "reason": "nothing to release"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"] == {"ref": "o/n#12", "holdKind": None, "holdBy": None}
    async with db_session() as db:
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert "pm.hold.release" not in types and "pm.hold.set" not in types


@pytest.mark.asyncio
async def test_set_hold_rejects_a_kind_the_agent_may_not_set(isolated_db):
    from server.db import db_session
    from server.db.models import WorkItem
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        db.add(WorkItem(repo="o/n", number=12, kind="issue", body="b", triage_state="classified"))
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "set_hold")
    out = await tool.handler({"kind": "owner_action", "reason": "needs the owner"})
    assert out.get("is_error") is True
    assert "owner_action" in out["content"][0]["text"] or "agent" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_set_hold_may_not_overwrite_a_persons_owner_action_hold(isolated_db):
    """An agent-settable kind written OVER a person's ``owner_action`` would release
    it, so the tool must refuse (final-review F1) and the row must not move."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.pm.holds import set_hold as pm_set_hold
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        wi = WorkItem(repo="o/n", number=12, kind="issue", body="b", triage_state="classified")
        pm_set_hold(wi, kind="owner_action", reason="owner only", actor="tony@example.com",
                    now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        db.add(wi)
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "set_hold")
    out = await tool.handler({"kind": "evidence", "reason": "correlate with #2"})
    assert out.get("is_error") is True
    assert "owner_action" in out["content"][0]["text"]
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem))).scalar_one()
        assert (wi.hold_kind, wi.hold_by) == ("owner_action", "tony@example.com")
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert "pm.hold.set" not in types


@pytest.mark.asyncio
async def test_set_hold_without_a_ref_or_origin_is_a_tool_error(isolated_db):
    from server.sessions.tools.tools import build_session_tools
    rec = SimpleNamespace(id="s1", origin_ref=None)
    tool = _tool(build_session_tools(_ctx(record=rec, signalling_tools=True)), "set_hold")
    out = await tool.handler({"kind": "evidence", "reason": "needs a repro"})
    assert out.get("is_error") is True
    assert "ref" in out["content"][0]["text"]


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
    from viberpit import brand
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


def test_finishing_descriptions_name_the_armed_pr_as_finished_and_refuse_status_only_hand_backs():
    """Prod 2026-09-25 (session 2971d6a6): with one PR merged and the next ready, armed
    and CI still running, the session called `hand_back` with the reason "Status only,
    no action needed". `mark_complete` said "never while … waiting on something" and
    `hand_back` never said a hand-back has to ask for anything, so a session waiting on
    CI was left with no signal it was allowed to use. The clauses are the SAME constants
    the marker instructions carry (test_hand_back_marker.py), so the two worlds agree."""
    from server.sessions.completion import FINISHED_STATE_CLAUSE, STATUS_ONLY_CLAUSE
    from server.sessions.tools.tools import build_session_tools
    desc = {t.name: t.description
            for t in build_session_tools(_ctx(signalling_tools=True))}
    assert FINISHED_STATE_CLAUSE in desc["mark_complete"]
    assert "waiting on something" not in desc["mark_complete"]
    assert STATUS_ONLY_CLAUSE in desc["hand_back"]


def test_set_hold_is_mounted_with_the_signalling_tools():
    from server.sessions.tools.registry import mounted_names
    assert "set_hold" in mounted_names(signalling_tools=True, self_model_switch=False)
    assert "set_hold" not in mounted_names(signalling_tools=False, self_model_switch=False)


def test_propose_decision_is_mounted_with_the_signalling_tools():
    """Spec 2026-09-18 §6. It rides with ``set_hold`` — no extra engine capability —
    because the two are the pair a session picks between when it cannot proceed."""
    from server.sessions.tools.registry import mounted_names
    assert "propose_decision" in mounted_names(signalling_tools=True, self_model_switch=False)
    assert "propose_decision" not in mounted_names(signalling_tools=False,
                                                   self_model_switch=False)


_OPTIONS = [{"key": "stop", "label": "Stop the turn", "detail": "Archive immediately."},
            {"key": "finish", "label": "Let it finish", "detail": "Archive on completion."}]


@pytest.mark.asyncio
async def test_propose_decision_writes_the_proposal_and_the_hold_as_the_session(isolated_db):
    """The handler is a thin tap on ``server/pm/decisions.py:propose`` (constitution
    VI): the proposal row, the ``decision`` hold and BOTH audit rows come from the
    service the HTTP route calls, attributed to ``session:<id>``, and the tool's own
    answer tells the model how to end the turn."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, DecisionProposal, WorkItem
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        db.add(WorkItem(repo="o/n", number=12, kind="issue", body="b",
                        triage_state="classified"))
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "propose_decision")
    out = await tool.handler({"options": _OPTIONS, "recommended_key": "finish",
                              "rationale": "Stopping a turn mid-flight loses the work."})
    assert out.get("is_error") is not True
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["ref"] == "o/n#12" and data["status"] == "pending"
    assert 'mark_no_pr("awaiting-decision"' in data["next"]
    async with db_session() as db:
        prop = (await db.execute(select(DecisionProposal))).scalar_one()
        assert prop.proposed_by == "session:s1" and prop.session_id == "s1"
        assert prop.recommended_key == "finish"
        assert [o["key"] for o in prop.options] == ["stop", "finish"]
        assert data["proposalId"] == prop.id
        wi = (await db.execute(select(WorkItem))).scalar_one()
        assert (wi.hold_kind, wi.hold_by) == ("decision", "session:s1")
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert {"pm.decision.propose", "pm.hold.set"} <= types


@pytest.mark.asyncio
async def test_propose_decision_refuses_a_second_pending_proposal(isolated_db):
    """One pending decision per item: the second call is a corrective message to the
    model, not a second park — and nothing new is written."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import DecisionProposal, WorkItem
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        db.add(WorkItem(repo="o/n", number=12, kind="issue", body="b",
                        triage_state="classified"))
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "propose_decision")
    args = {"options": _OPTIONS, "recommended_key": "stop", "rationale": "Two ways."}
    assert (await tool.handler(args)).get("is_error") is not True
    out = await tool.handler(args)
    assert out["is_error"] is True
    from server.pm.decisions import PENDING_CONFLICT_MESSAGE
    assert PENDING_CONFLICT_MESSAGE in out["content"][0]["text"]
    async with db_session() as db:
        assert len((await db.execute(select(DecisionProposal))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_propose_decision_rejects_a_malformed_proposal_without_parking(isolated_db):
    """The VALIDATION is the service's (2-6 options, a recommendation among them) —
    the handler only has to turn its refusal into the model's corrective message,
    and the item must be left unheld."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import WorkItem
    from server.sessions.tools.tools import build_session_tools
    async with db_session() as db:
        db.add(WorkItem(repo="o/n", number=12, kind="issue", body="b",
                        triage_state="classified"))
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "propose_decision")
    out = await tool.handler({"options": _OPTIONS, "recommended_key": "neither",
                              "rationale": "why"})
    assert out["is_error"] is True and "recommended_key" in out["content"][0]["text"]
    async with db_session() as db:
        assert (await db.execute(select(WorkItem))).scalar_one().hold_kind is None


@pytest.mark.asyncio
async def test_propose_decision_without_an_issue_ref_is_a_tool_error(isolated_db):
    """A decision parks THIS session's issue; a session dispatched without one has
    nothing to park, and the model is pointed at hand_back instead."""
    from server.sessions.tools.tools import build_session_tools
    rec = SimpleNamespace(id="s1", origin_ref=None)
    tool = _tool(build_session_tools(_ctx(record=rec, signalling_tools=True)),
                 "propose_decision")
    out = await tool.handler({"options": _OPTIONS, "recommended_key": "stop",
                              "rationale": "why"})
    assert out["is_error"] is True and "hand_back" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_propose_decision_on_an_unmirrored_issue_is_a_tool_error(isolated_db):
    """The ref resolve is the one ``set_hold`` uses (``holds.work_item_for_ref``), so
    both tools answer "that issue is not mirrored" in the same words."""
    from server.sessions.tools.tools import build_session_tools
    tool = _tool(build_session_tools(_ctx(signalling_tools=True)), "propose_decision")
    out = await tool.handler({"options": _OPTIONS, "recommended_key": "stop",
                              "rationale": "why"})
    assert out["is_error"] is True and "mirrored" in out["content"][0]["text"]
