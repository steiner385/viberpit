"""Asks answered in the Claude app (Remote Control) resolve in ViberPit too.

When a tool-permission prompt, a plan approval or an AskUserQuestion is answered in the
Claude web/desktop app, the CLI settles it itself and sends the host a
``control_cancel_request``; the SDK (``_internal/query.py``) cancels our ``can_use_tool``
handler task. The tests simulate that by cancelling the task mid-wait and pin that the
card resolves (marked ``via="claude_app"``), the status leaves waiting_input, the
registry holds nothing pending, and the CancelledError still propagates — then that the
outcome is read off the tool_result that follows.
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import ToolPermissionContext

from server.sessions import answered_elsewhere as ae
from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

_APP_ANSWER = ('Your questions have been answered: "Which database?"="Postgres", '
               '"Which features?"="Auth, Billing". You can now continue with these '
               'answers in mind.')


class _StuckTurn(FakeSDKClient):
    """The first query opens a turn that stays open (the CLI parked on the ask)."""

    async def query(self, prompt: str):
        self.sent.append(prompt)
        if len(self.sent) == 1:
            self._deliver([f.stream_start(uuid="t1")])
        else:
            self._deliver([f.result(uuid=f"r{len(self.sent)}")])


def _make(sdk, reg):
    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return sdk

    return ClaudeAgentDriver("rc-ans", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=factory, registry=reg, identity="me")


def _ctx(tool_use_id: str) -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=tool_use_id)


async def _wait_for(pred, what: str):
    for _ in range(400):
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _started(sdk, reg):
    d = _make(sdk, reg)
    q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(q)
    await d.start()
    await d.send_prompt("go")
    await asyncio.sleep(0.02)
    return d, q


async def _cancelled(task: asyncio.Task) -> bool:
    task.cancel()   # what the SDK does on control_cancel_request
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


def test_permission_answered_in_claude_app_resolves_the_card_and_reads_allow():
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d, q = await _started(sdk, reg)
        task = asyncio.create_task(d._can_use_tool("Bash", {"command": "ls"}, _ctx("tu-1")))
        await _wait_for(lambda: reg.pending(d.session_id), "the permission card")
        assert d.awaiting_user_input
        propagated = await _cancelled(task)
        await asyncio.sleep(0.02)   # the scheduled status refresh
        first = _drain(q)
        pending_after = reg.pending(d.session_id)
        awaiting_after = d.awaiting_user_input
        # The tool ran (allowed in the app): its result streams in.
        sdk.inject([f.tool_use("tu-1", "Bash", {"command": "ls"}),
                    f.user_tool_result("tu-1", "file.txt")])
        await asyncio.sleep(0.05)
        second = _drain(q)
        await d.aclose()
        return propagated, first, pending_after, awaiting_after, second

    propagated, first, pending_after, awaiting_after, second = asyncio.run(run())
    assert propagated, "the CancelledError must propagate to the SDK"
    assert pending_after == [] and awaiting_after is False
    resolved = [e for e in first if isinstance(e, ev.PermissionResolved)]
    assert len(resolved) == 1
    assert resolved[0].via == ae.VIA_CLAUDE_APP and resolved[0].allowed is None
    assert resolved[0].to_wire()["via"] == "claude_app"
    statuses = [e.state for e in first if isinstance(e, ev.Status)]
    assert statuses and statuses[-1] != "waiting_input"
    outcome = [e for e in second if isinstance(e, ev.PermissionResolved)]
    assert len(outcome) == 1
    assert outcome[0].perm_id == resolved[0].perm_id
    assert outcome[0].allowed is True and outcome[0].via == ae.VIA_CLAUDE_APP


def test_permission_denied_in_claude_app_reads_deny_from_the_cli_text():
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d, q = await _started(sdk, reg)
        task = asyncio.create_task(d._can_use_tool("ExitPlanMode", {"plan": "x"}, _ctx("tu-p")))
        await _wait_for(lambda: reg.pending(d.session_id), "the plan card")
        await _cancelled(task)
        sdk.inject([f.user_tool_result(
            "tu-p", "The user doesn't want to proceed with this tool use. The tool use was "
                    "rejected (eg. if it was a file edit, the new_string was NOT written to "
                    "the file).", is_error=True)])
        await asyncio.sleep(0.05)
        events = _drain(q)
        await d.aclose()
        return events

    events = asyncio.run(run())
    resolved = [e for e in events if isinstance(e, ev.PermissionResolved)]
    assert [(r.allowed, r.via) for r in resolved] == [(None, "claude_app"), (False, "claude_app")]


def test_question_answered_in_claude_app_resolves_and_reads_the_answers():
    sdk = _StuckTurn([])
    reg = PermissionRegistry()
    questions = [
        {"question": "Which database?", "header": "DB",
         "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
        {"question": "Which features?", "header": "Features", "multiSelect": True,
         "options": [{"label": "Auth"}, {"label": "Billing"}, {"label": "Search"}]},
    ]

    async def run():
        d, q = await _started(sdk, reg)
        task = asyncio.create_task(
            d._can_use_tool("AskUserQuestion", {"questions": questions}, _ctx("tu-q")))
        await _wait_for(lambda: len(d.pending_questions()) == 2, "the question cards")
        assert d.awaiting_user_input
        propagated = await _cancelled(task)
        await asyncio.sleep(0.02)
        first = _drain(q)
        left = d.pending_questions()
        awaiting = d.awaiting_user_input
        sdk.inject([f.user_tool_result("tu-q", _APP_ANSWER)])
        await asyncio.sleep(0.05)
        second = _drain(q)
        await d.aclose()
        return propagated, first, left, awaiting, second

    propagated, first, left, awaiting, second = asyncio.run(run())
    assert propagated and left == [] and awaiting is False
    resolved = [e for e in first if isinstance(e, ev.QuestionResolved)]
    assert len(resolved) == 2
    assert all(r.via == "claude_app" and r.selected is None for r in resolved)
    statuses = [e.state for e in first if isinstance(e, ev.Status)]
    assert statuses and statuses[-1] not in ("waiting_input", "waiting_question")
    answers = {r.question_id: r.selected for r in second if isinstance(r, ev.QuestionResolved)}
    ids = [r.question_id for r in resolved]
    assert answers == {ids[0]: ["Postgres"], ids[1]: ["Auth", "Billing"]}


def test_teardown_cancel_is_not_labelled_claude_app():
    """A cancel while the driver is closing is our own teardown: the card still clears,
    but nobody answered it in the Claude app."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d, q = await _started(sdk, reg)
        task = asyncio.create_task(d._can_use_tool("Bash", {"command": "ls"}, _ctx("tu-t")))
        await _wait_for(lambda: reg.pending(d.session_id), "the permission card")
        d._closing = True   # the close path, before its deny_all reaches the card
        await _cancelled(task)
        events = _drain(q)
        d._closing = False
        await d.aclose()
        return events, reg.pending(d.session_id)

    events, pending = asyncio.run(run())
    assert pending == []
    resolved = [e for e in events if isinstance(e, ev.PermissionResolved)]
    assert len(resolved) == 1 and resolved[0].via is None and resolved[0].allowed is False
    assert "via" not in resolved[0].to_wire()


def test_cancel_after_a_viberpit_decision_reports_that_decision():
    """The decision landed (registry settled) and the cancel raced in before the
    coroutine resumed: report the real decision, not an app answer."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d, q = await _started(sdk, reg)
        task = asyncio.create_task(d._can_use_tool("Bash", {"command": "ls"}, _ctx("tu-r")))
        await _wait_for(lambda: reg.pending(d.session_id), "the permission card")
        pid = reg.pending(d.session_id)[0]["permId"]
        assert reg.resolve(pid, "allow", "me")
        await _cancelled(task)
        events = _drain(q)
        await d.aclose()
        return events

    events = asyncio.run(run())
    resolved = [e for e in events if isinstance(e, ev.PermissionResolved)]
    assert [(r.allowed, r.via) for r in resolved] == [(True, None)]


@pytest.mark.parametrize("ok,content,allowed", [
    (True, "done", True),
    (False, "Exit code 1\nboom", True),     # the tool ran and failed — it was allowed
    (False, "Permission for this tool use was denied. The tool use was rejected", False),
    (False, [{"type": "text", "text": "The user doesn't want to proceed with this tool "
                                       "use. To tell you how to proceed, the user said:\nno"}],
     False),
])
def test_permission_allowed_reading(ok, content, allowed):
    assert ae.permission_allowed(ok, content) is allowed


def test_question_answers_reading():
    qs = [{"question": "Which database?", "options": [{"label": "Postgres"}]},
          {"question": "Which features?", "multiSelect": True,
           "options": [{"label": "Auth"}, {"label": "Billing"}]},
          {"question": "Missing?", "options": []}]
    assert ae.question_answers(_APP_ANSWER, qs) == [["Postgres"], ["Auth", "Billing"], None]
    # Free text with a comma stays whole; notes after the answer are not part of it.
    text = 'The user answered: "Which database?"="mine, really" notes: hi. Read the answers'
    assert ae.question_answers(text, qs[:1]) == [["mine, really"]]
    assert ae.question_answers('"Which database?"=(no option selected)', qs[:1]) == [None]
    assert ae.question_answers("The user did not answer the questions.", qs[:1]) == [None]


def test_reattach_snapshot_carries_the_resolution_after_the_card():
    """A mid-turn reattach re-delivers the turn's buffered asks: the resolution must
    follow the card there, and the registries offer nothing for the bridge to re-emit
    as pending."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d, _q = await _started(sdk, reg)
        task = asyncio.create_task(d._can_use_tool("Bash", {"command": "ls"}, _ctx("tu-s")))
        await _wait_for(lambda: reg.pending(d.session_id), "the permission card")
        await _cancelled(task)
        buffered = [e for e in d._turn_buffer
                    if isinstance(e, (ev.PermissionRequest, ev.PermissionResolved))]
        pending = (reg.pending(d.session_id), d.pending_questions())
        await d.aclose()
        return buffered, pending

    buffered, pending = asyncio.run(run())
    assert pending == ([], [])
    assert [type(e).__name__ for e in buffered] == ["PermissionRequest", "PermissionResolved"]
    assert buffered[1].via == "claude_app"
