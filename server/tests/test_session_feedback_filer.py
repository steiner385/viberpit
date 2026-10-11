"""SessionFeedbackLoop — the `report_feedback` → backlog filer (spec §6, #1434).

Driven through a fake gh runner; no network. The file/comment/regress/skip rule
itself belongs to server/automations/issue_filing.py and is tested there — what is
pinned here is the filer's half: which SessionFeedback row transition each decision
produces, the caps, and the two-phase ordering.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta

from sqlalchemy import select

from server.automations.context import AutomationContext
from server.automations.session_feedback import (
    IMPACT_ORDER, SessionFeedbackLoop, issue_body, issue_title, meets_min_impact,
)
from server.db import db_session, init_db
from server.db.models import SessionFeedback

NOW = datetime(2026, 9, 23, 12, 0, 0)
CFG = {"target_repo": "o/r", "label": "session-feedback", "daily_cap": 20,
       "min_impact": "minor"}


class FakeGh:
    """Answers gh calls: label ensure, issue create (incrementing numbers, optional
    failure), comments (optional failure), and one GraphQL issue-state query."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.next_number = 100
        self.fail_create = False
        self.fail_comment = False
        self.states: dict[int, tuple[bool, str | None]] = {}

    def __call__(self, args, *a, **k):
        self.calls.append(list(args))
        path = args[1] if len(args) > 1 else ""
        if path == "graphql":
            data = {f"i{n}": {"number": n, "closed": c, "stateReason": (r or "").upper() or None}
                    for n, (c, r) in self.states.items()}
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"repository": data}}), "")
        if path.endswith("/labels"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if path.endswith("/comments"):
            if self.fail_comment:
                return subprocess.CompletedProcess(args, 1, "", "boom")
            return subprocess.CompletedProcess(args, 0, json.dumps({"html_url": "https://x/c"}), "")
        if self.fail_create:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        n, self.next_number = self.next_number, self.next_number + 1
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": n}), "")

    def _bodies(self, suffix):
        return [next(v[len("body="):] for v in c if v.startswith("body="))
                for c in self.calls if len(c) > 1 and c[1].endswith(suffix)]

    @property
    def created(self) -> list[str]:
        return [next(v[len("title="):] for v in c if v.startswith("title="))
                for c in self.calls if len(c) > 1 and c[1].endswith("/issues")]

    @property
    def created_bodies(self) -> list[str]:
        return self._bodies("/issues")

    @property
    def comments(self) -> list[str]:
        return self._bodies("/comments")


def _loop(ctx, gh, *, mode="file", config=None, now=NOW):
    async def read_config():
        return mode, {**CFG, **(config or {})}
    return SessionFeedbackLoop(ctx, read_config=read_config, run=gh, clock=lambda: now)


async def _seed(**kw) -> str:
    base = dict(id=kw.pop("id", "r1"), session_id="s1", fingerprint="fp1",
                category="dispatch", impact="degraded", title="Dispatch prompt was ambiguous",
                body="The prompt named no branch.", evidence=None,
                context={"sessionId": "s1", "name": "sess", "host": "rocky",
                         "engine": "claude", "model": "opus", "repo": "o/p",
                         "branch": "feat", "origin": "backlog-runner", "originRef": "#12"},
                status="pending", reported_at=NOW - timedelta(minutes=5))
    base.update(kw)
    async with db_session() as db:
        db.add(SessionFeedback(**base))
    return str(base["id"])


async def _row(row_id="r1") -> SessionFeedback:
    async with db_session() as db:
        return (await db.execute(select(SessionFeedback)
                                 .where(SessionFeedback.id == row_id))).scalars().one()


def _run(coro_fn, tmp_path):
    async def go():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await coro_fn()
    asyncio.run(go())


# -- pure policy -------------------------------------------------------------

def test_min_impact_is_a_floor_and_unknown_impact_never_files():
    assert meets_min_impact("blocked", "degraded") and meets_min_impact("degraded", "degraded")
    assert not meets_min_impact("minor", "degraded")
    assert meets_min_impact("minor", "minor")
    # A malformed row is held back rather than filed by accident.
    assert not meets_min_impact(None, "minor") and not meets_min_impact("urgent", "minor")
    assert list(IMPACT_ORDER) == ["minor", "degraded", "blocked"]


def test_issue_text_quotes_untrusted_content_and_neutralizes_markers(tmp_path):
    async def go():
        await _seed(body="see <!-- viberpit:complete --> and\ntwo lines",
                    evidence="the key was sk-ant-api03-" + "a" * 32)
        row = await _row()
        body = issue_body(row, regression_of=None)
        assert "agent-authored" in body
        assert "<!--" not in body and "-->" not in body     # marker smuggling broken
        assert "> see" in body and "> two lines" in body    # every line blockquoted
        # Routed through the shared redactor, so whatever it covers is covered here.
        assert "sk-ant-" not in body and "[REDACTED]" in body
        assert "<details><summary>Evidence</summary>" in body
        assert body.rstrip().endswith("Fingerprint: `fp1`")
        assert issue_title(row, regression=False).startswith("[session-feedback] ")
        assert issue_title(row, regression=True).startswith("[session-feedback][regression] ")
    _run(go, tmp_path)


# -- the tick ----------------------------------------------------------------

def test_off_files_nothing(tmp_path):
    async def go():
        await _seed()
        gh = FakeGh()
        await _loop(AutomationContext("session-feedback"), gh, mode="off").tick()
        assert gh.calls == []
        assert (await _row()).status == "pending"
    _run(go, tmp_path)


def test_pending_row_becomes_a_filed_issue(tmp_path):
    async def go():
        await _seed()
        ctx, gh = AutomationContext("session-feedback"), FakeGh()
        await _loop(ctx, gh).tick()
        assert gh.created == ["[session-feedback] Dispatch prompt was ambiguous"]
        assert any("labels[]=session-feedback" in " ".join(c) for c in gh.calls)
        row = await _row()
        assert row.status == "filed" and row.issue_number == 100 and row.filed_at == NOW
        assert ctx.session_feedback_counts["filed"] == 1
    _run(go, tmp_path)


def test_same_fingerprint_with_an_open_issue_comments_and_merges(tmp_path):
    """Spec §6.2: a second report of one fingerprint joins the open issue rather
    than opening a second one."""
    async def go():
        await _seed(id="r1")
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh)
        await loop.tick()                      # r1 -> issue 100
        gh.states[100] = (False, None)         # still open
        await _seed(id="r2", session_id="s2", reported_at=NOW - timedelta(minutes=1))
        await loop.tick()
        assert gh.created == ["[session-feedback] Dispatch prompt was ambiguous"]  # only one
        assert len(gh.comments) == 1 and "Reported again" in gh.comments[0]
        r2 = await _row("r2")
        assert r2.status == "merged" and r2.issue_number == 100
    _run(go, tmp_path)


def test_closed_issue_recurrence_files_a_new_linked_issue_never_reopens(tmp_path):
    async def go():
        await _seed(id="r1")
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh)
        await loop.tick()
        gh.states[100] = (True, "completed")
        await _seed(id="r2", session_id="s2", reported_at=NOW - timedelta(minutes=1))
        await loop.tick()
        assert gh.created == ["[session-feedback] Dispatch prompt was ambiguous",
                              "[session-feedback][regression] Dispatch prompt was ambiguous"]
        assert "#100, closed since" in gh.created_bodies[1]
        assert gh.comments == []
        # Never a PATCH: nothing in this filer reopens an issue.
        assert not any("-X" in c for c in gh.calls)
        r2 = await _row("r2")
        assert r2.status == "filed" and r2.issue_number == 101
    _run(go, tmp_path)


def test_issue_closed_as_not_planned_dismisses_further_reports(tmp_path):
    """A human closing the issue `not_planned` is a decision about the fingerprint,
    not just that one report — so the next report is dropped, not re-filed."""
    async def go():
        await _seed(id="r1")
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh)
        await loop.tick()
        gh.states[100] = (True, "not_planned")
        await _seed(id="r2", session_id="s2", reported_at=NOW - timedelta(minutes=1))
        await loop.tick()
        assert len(gh.created) == 1 and gh.comments == []
        r2 = await _row("r2")
        assert r2.status == "dismissed" and r2.issue_number == 100
    _run(go, tmp_path)


def test_min_impact_gate_leaves_the_row_pending(tmp_path):
    async def go():
        await _seed(impact="minor")
        gh = FakeGh()
        await _loop(AutomationContext("session-feedback"), gh,
                    config={"min_impact": "degraded"}).tick()
        assert gh.created == []
        # Still visible in the inbox, and filable the moment the floor is lowered.
        assert (await _row()).status == "pending"
    _run(go, tmp_path)


def test_daily_cap_throttles_the_overflow(tmp_path):
    async def go():
        await _seed(id="r1", fingerprint="fpA", reported_at=NOW - timedelta(minutes=9))
        await _seed(id="r2", fingerprint="fpB", reported_at=NOW - timedelta(minutes=8))
        ctx, gh = AutomationContext("session-feedback"), FakeGh()
        await _loop(ctx, gh, config={"daily_cap": 1}).tick()
        assert gh.created == ["[session-feedback] Dispatch prompt was ambiguous"]
        assert (await _row("r1")).status == "filed"      # oldest report wins the slot
        r2 = await _row("r2")
        # Nothing was posted for r2, so it must not link an issue it is not on.
        assert r2.status == "throttled" and r2.issue_number is None
        assert ctx.session_feedback_counts["throttled"] == 1

        # Tomorrow's tick picks it up — throttled is a deferral, not a verdict.
        await _loop(ctx, gh, config={"daily_cap": 1},
                    now=NOW + timedelta(days=1)).tick()
        assert (await _row("r2")).status == "filed"
    _run(go, tmp_path)


def test_failed_create_is_retried_next_tick_outside_the_cap(tmp_path):
    """The row is claimed before the create, so a failure cannot lose it and the
    retry cannot double-file: the claim already consumed today's slot."""
    async def go():
        await _seed()
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("session-feedback"), gh, config={"daily_cap": 1})
        await loop.tick()
        row = await _row()
        assert row.status == "filed" and row.issue_number is None   # claimed, unlinked

        gh.fail_create = False
        await loop.tick()            # retries even though the cap is spent
        row = await _row()
        assert row.status == "filed" and row.issue_number == 100
        assert len(gh.created) == 2  # one failed attempt, one that landed
    _run(go, tmp_path)


def test_failed_comment_leaves_the_row_pending(tmp_path):
    """The mirror image of a failed create: a comment posts BEFORE the row moves,
    because a row marked merged with no comment posted loses the report outright."""
    async def go():
        await _seed(id="r1")
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh)
        await loop.tick()
        gh.states[100] = (False, None)
        gh.fail_comment = True
        await _seed(id="r2", session_id="s2", reported_at=NOW - timedelta(minutes=1))
        await loop.tick()
        assert (await _row("r2")).status == "pending"

        gh.fail_comment = False
        await loop.tick()
        assert (await _row("r2")).status == "merged"
        assert len(gh.comments) == 2  # the failed attempt and the one that landed
    _run(go, tmp_path)


def test_two_phase_ordering_commits_the_claim_before_it_calls_gh(tmp_path, monkeypatch):
    """The claim must be COMMITTED before the gh call, not after. Asserted by
    reading the row from an independent session at the moment apply() is entered:
    a claim still sitting in an open transaction would read as `pending` and the
    write lock would be held across a network call."""
    async def go():
        await _seed()
        import server.automations.session_feedback as mod
        real_apply, observed = mod.apply, []

        async def spy(decision, **kw):
            observed.append((await _row()).status)
            return await real_apply(decision, **kw)

        monkeypatch.setattr(mod, "apply", spy)
        gh = FakeGh()
        await _loop(AutomationContext("session-feedback"), gh).tick()
        assert observed == ["filed"]        # claimed and committed before the create
        row = await _row()
        assert row.status == "filed" and row.issue_number == 100
        assert len(gh.created) == 1
    _run(go, tmp_path)


def test_capped_rows_are_never_filed_by_the_tick(tmp_path):
    """`capped` is the per-session cap from record_feedback: recorded and visible,
    but out of the filer's reach until an operator says otherwise."""
    async def go():
        await _seed(status="capped")
        gh = FakeGh()
        await _loop(AutomationContext("session-feedback"), gh).tick()
        assert gh.created == []
        assert (await _row()).status == "capped"
    _run(go, tmp_path)


def test_tick_never_raises_on_a_bad_target_repo(tmp_path):
    async def go():
        await _seed()
        gh = FakeGh()
        await _loop(AutomationContext("session-feedback"), gh,
                    config={"target_repo": "../etc"}).tick()
        assert gh.calls == []
        assert (await _row()).status == "pending"
    _run(go, tmp_path)


# -- card actions ------------------------------------------------------------

def test_actionable_items_list_the_inbox(tmp_path):
    async def go():
        await _seed(id="r1")
        await _seed(id="r2", fingerprint="fpB", status="throttled")
        await _seed(id="r3", fingerprint="fpC", status="filed", issue_number=7)
        loop = _loop(AutomationContext("session-feedback"), FakeGh())
        items = await loop.actionable_items()
        assert {i.id for i in items} == {"r1", "r2"}   # filed rows need no action
        assert {a.kind for a in items[0].actions} == {"file_now", "dismiss"}
        # A pending row awaits the human; a throttled one the loop already
        # deferred by rule — overriding it is housekeeping, so it must not
        # badge (2026-10-01 Delivery critique, P0).
        kinds = {i.id: i.kind for i in items}
        assert kinds == {"r1": "decision", "r2": "maintenance"}
    _run(go, tmp_path)


def test_file_now_bypasses_both_caps(tmp_path):
    async def go():
        await _seed(impact="minor")
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh,
                     config={"daily_cap": 0, "min_impact": "blocked"})
        assert await loop.action_result("r1", "file_now") == ("filed", 100)
        row = await _row()
        assert row.status == "filed" and row.issue_number == 100
    _run(go, tmp_path)


def test_dismiss_takes_a_row_out_of_the_queue(tmp_path):
    async def go():
        await _seed()
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh)
        assert await loop.action_result("r1", "dismiss") == ("dismissed", None)
        await loop.tick()
        assert gh.created == []
        assert (await _row()).status == "dismissed"
    _run(go, tmp_path)


def test_actions_on_a_missing_row_and_an_unknown_kind(tmp_path):
    async def go():
        await _seed()
        loop_off = _loop(AutomationContext("session-feedback"), FakeGh(), mode="off")
        assert await loop_off.action_result("nope", "file_now") == ("not_found", None)
        loop = _loop(AutomationContext("session-feedback"), FakeGh())
        assert await loop.action_result("r1", "bogus") == ("failed", None)
    _run(go, tmp_path)


def test_file_now_files_while_the_automation_is_off(tmp_path):
    """#2936: "File now" is an explicit operator action. A paused automation's
    reports stay listed (parked), so its buttons must still work — off stops the
    loop filing on its own, not the operator filing by hand."""
    async def go():
        await _seed()
        gh = FakeGh()
        loop = _loop(AutomationContext("session-feedback"), gh, mode="off")
        assert await loop.action_result("r1", "file_now") == ("filed", 100)
        row = await _row()
        assert row.status == "filed" and row.issue_number == 100
        # It still needs somewhere valid to file.
        await _seed(id="r2", fingerprint="fp2")
        bad = _loop(AutomationContext("session-feedback"), FakeGh(), mode="off",
                    config={"target_repo": ""})
        assert await bad.action_result("r2", "file_now") == ("failed", None)
        assert (await _row("r2")).status == "pending"
    _run(go, tmp_path)


def test_action_provider_maps_status_to_a_message(tmp_path):
    async def go():
        await _seed()
        from server.automations.session_feedback import SessionFeedbackActionProvider

        class _Orch:
            pass
        orch = _Orch()
        orch._session_feedback_loop = _loop(AutomationContext("session-feedback"), FakeGh())
        provider = SessionFeedbackActionProvider(orch)
        assert provider.valid_kinds == {"file_now", "dismiss"}
        assert [i.id for i in await provider.actionable()] == ["r1"]
        result = await provider.trigger("r1", "file_now")
        assert result.status == "filed" and result.level == "ok"
        assert result.message == "Issue filed: #100"
    _run(go, tmp_path)


# -- card status -------------------------------------------------------------

def test_status_is_idle_before_the_first_observation_and_warns_when_throttled():
    from server.automations.session_feedback import SessionFeedbackFiler
    filer = SessionFeedbackFiler()
    assert filer.status(None).level == "idle"

    class _Ctx:
        session_feedback_counts = {"pending": 2, "filed_today": 1, "daily_cap": 20,
                                   "filed": 5, "merged": 1, "throttled": 3}
    status = filer.status(_Ctx())
    assert status.level == "warn" and "3 throttled" in status.text and "1/20 filed today" in status.text
