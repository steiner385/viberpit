"""Completion-marker auto-archive is a universal rule, not a backlog-runner privilege.

Any session that emits ``<!-- ccswitch:complete -->`` (derived status ``complete``)
is auto-archived once the completion bar (``met_bar``) confirms the goal is genuinely
met — regardless of origin and regardless of the autopilot mode. When the bar is NOT
met (e.g. an open-unarmed PR), the session is nudged toward the remaining work instead
of archiving; the nudge opens a fresh turn, so the session no longer rests in
``complete``. A per-session nudge cap bounds an ill-behaved agent that re-signals
completion without making progress.

This generalises the old backlog-runner-only path (see test_backlog_autoarchive.py).
"""
from __future__ import annotations

import asyncio
import types

from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(sid="s1", *, origin=None, repo="o/r", branch="feat/x", initial_prompt=None):
    r = SessionRecord(id=sid, name="run", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                      cols=80, rows=24, cwd="/tmp", repo=repo, branch=branch)
    r.origin = origin
    r.initial_prompt = initial_prompt
    return r


def _orch(records, pr_info=None):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records
    orch._auto_archiving = set()
    orch._backlog_ctx = AutomationContext("backlog-runner")

    archived: list[dict] = []
    nudged: list[str] = []
    resumed: list[str] = []

    async def _spy_archive(session_id, *, archived_by="user", context=None):
        archived.append({"session_id": session_id, "archived_by": archived_by,
                         "context": context})

    async def _spy_send_prompt(session_id, prompt, *, origin=None):
        nudged.append(prompt)
        return "sent"

    async def _spy_ensure_driver(session_id, identity="system"):
        # The nudge path resumes the session before sending (the driver was released
        # by the `complete` transition). Stubbed here so these unit-level stubs, which
        # have no _drivers registry, exercise the nudge rather than blowing up in
        # ensure_driver — a resumed session is exactly what production hands it.
        resumed.append(session_id)
        return object()

    orch.archive_session = _spy_archive  # type: ignore[method-assign]
    orch.ensure_driver = _spy_ensure_driver  # type: ignore[method-assign]
    orch.send_prompt = _spy_send_prompt  # type: ignore[method-assign]
    orch.pr_for = lambda repo, branch: pr_info  # type: ignore[method-assign]
    # Unit-level orch, no DB: pin the app-wide auto-archive toggle enabled so the
    # gate reads a known value (its own read is covered in test_settings.py; the
    # disabled path is exercised by test_global_disable_blocks_autoarchive, which
    # overrides this).
    async def _autoarchive_enabled() -> bool:
        return True
    orch._read_auto_archive_enabled = _autoarchive_enabled  # type: ignore[method-assign]
    orch.resumed_for_nudge = resumed
    return orch, archived, nudged


def _armed_pr():
    return types.SimpleNamespace(
        number=42, state="open", is_draft=False, auto_merge_enabled=True,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/42",
        docs_only=False)


def _unarmed_pr():
    return types.SimpleNamespace(
        number=99, state="open", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/99",
        docs_only=False)


def _merged_pr():
    return types.SimpleNamespace(
        number=7, state="merged", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/7",
        docs_only=False)


async def _drain():
    # Let the scheduled _auto_archive_completed task run to completion.
    for _ in range(4):
        await asyncio.sleep(0)


def test_human_complete_with_armed_pr_is_archived():
    """A human-origin session that self-declares complete with an armed PR archives —
    the old code returned early for any non-backlog origin, so it never did."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
        assert nudged == []
    asyncio.run(run())


def test_human_complete_no_repo_is_archived():
    """A repo-less (non-code / research) session that completes archives immediately —
    met_bar returns met for a non-code goal with no PR expected."""
    async def run():
        rec = _rec("s1", origin=None, repo=None, branch=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
        assert nudged == []
    asyncio.run(run())


def test_complete_with_merged_pr_non_backlog_origin_archived():
    """Any origin (here pr-doctor) with a merged PR archives on complete."""
    async def run():
        rec = _rec("s1", origin="pr-doctor")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_merged_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
    asyncio.run(run())


def test_human_complete_with_unarmed_pr_is_nudged_not_archived():
    """The readiness gate is universal: a completed session with an open-unarmed PR is
    nudged to arm it, NOT archived — even without an explicit --squash --auto step."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "open-unarmed PR is not the finished state"
        assert nudged, "must nudge toward arming the PR"
        assert "99" in nudged[0]  # remedy references the PR number
        # The `complete` transition already released the driver, so the nudge MUST
        # resume the session first — otherwise send_prompt returns the "no_driver"
        # sentinel and the corrective nudge is silently discarded (final review, C3).
        assert orch.resumed_for_nudge == ["s1"]
    asyncio.run(run())


def test_completion_archive_provenance_is_completion_for_non_backlog():
    """Non-backlog completion archives are stamped archived_by='completion' so the retro
    archive audit + reversal-learning (AUTO_ARCHIVE_PROVENANCE) still cover them."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived[0]["archived_by"] == "completion"
    asyncio.run(run())


def test_backlog_completion_archive_keeps_backlog_provenance():
    """Backlog-runner completion archives keep their existing archived_by provenance."""
    async def run():
        rec = _rec("s1", origin="backlog-runner")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived[0]["archived_by"] == "backlog-runner"
    asyncio.run(run())


def test_manual_unarchive_opt_out_blocks_any_origin():
    """The sticky manual-unarchive opt-out wins over the completion signal for every origin."""
    async def run():
        rec = _rec("s1", origin=None)
        rec.auto_archive_opt_out_reason = "manual-unarchive"
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert nudged == []
    asyncio.run(run())


def test_create_time_opt_out_blocks_completion_autoarchive():
    """The per-session "Auto-archive" toggle unticked at dispatch lands on the same
    sticky column as a manual unarchive, so the completion path leaves the session
    alone — no archive, no nudge — without a second gate."""
    async def run():
        from server.sessions.types import AUTO_ARCHIVE_OPT_OUT_CREATE
        rec = _rec("s1", origin=None)
        rec.auto_archive_opt_out_reason = AUTO_ARCHIVE_OPT_OUT_CREATE
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert nudged == []
    asyncio.run(run())


def test_repeated_not_ready_completes_stop_nudging_after_cap():
    """An agent that keeps re-signalling complete without arming its PR is nudged up to
    the cap, then left alone (no infinite nudge loop)."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())
        # Re-emit complete many times; each defer clears the guard so the next call runs.
        for _ in range(6):
            orch._maybe_autoarchive_completed("s1", "complete", rec)
            await _drain()
        assert archived == []
        from server.sessions.orchestrator import COMPLETE_NUDGE_CAP
        assert len(nudged) == COMPLETE_NUDGE_CAP, (
            f"nudges must be capped at {COMPLETE_NUDGE_CAP}, got {len(nudged)}")
    asyncio.run(run())


# --------------------------------------------------------- stuck-complete recheck
#
# Once `_complete_nudge_over_cap` gives up, a `complete` session has no other trigger
# back to the completion gate: `_autopilot_list_governed` deliberately excludes
# `complete` from autopilot's own periodic tick (to avoid a second decision path
# racing this deterministic one), and nothing re-emits `complete` for a session whose
# driver was never resumed. `_recheck_stuck_complete_sessions_once` (the always-on
# loop) and `recheck_stuck_complete_session` (the StaleOwnership resume-path trigger
# in ws.py) are the only two ways such a session ever leaves `complete` once its
# tracked PR/issue resolves after the give-up.

def test_stuck_complete_session_archives_once_pr_merges_after_nudge_cap():
    """A session nudged to COMPLETE_NUDGE_CAP on an open-unarmed PR is left parked at
    `complete` forever under the OLD behavior. Once the PR actually merges — well
    after the session went quiet, e.g. via the merge queue — the periodic sweep must
    catch it and archive; nothing else asks again."""
    async def run():
        rec = _rec("s1", origin=None)
        rec.status = "complete"
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())
        for _ in range(6):
            orch._maybe_autoarchive_completed("s1", "complete", rec)
            await _drain()
        assert archived == []
        from server.sessions.orchestrator import COMPLETE_NUDGE_CAP
        assert len(nudged) == COMPLETE_NUDGE_CAP, "must have given up nudging first"

        # The PR merges after the give-up.
        orch.pr_for = lambda repo, branch: _merged_pr()  # type: ignore[method-assign]
        await orch._recheck_stuck_complete_sessions_once()
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"], (
            "the periodic recheck must archive once met_bar is satisfied")
    asyncio.run(run())


def test_recheck_sweep_skips_a_session_still_inside_its_nudge_window():
    """A `complete` session that has not yet exhausted its nudge cap must NOT be
    touched by the periodic sweep — it already gets its own retry when the nudge's
    follow-up turn completes; double-poking it here would race that path."""
    async def run():
        rec = _rec("s1", origin=None)
        rec.status = "complete"
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert len(nudged) == 1
        nudged.clear()

        await orch._recheck_stuck_complete_sessions_once()
        await _drain()
        assert archived == [] and nudged == [], (
            "must not re-nudge or archive before the nudge cap is exhausted")
    asyncio.run(run())


def test_recheck_stuck_complete_session_ignores_non_complete_status():
    """`recheck_stuck_complete_session` — the direct StaleOwnership resume-path
    trigger — must no-op for any status other than `complete`. It must never
    resurrect a session that already archived or moved on to something else."""
    async def run():
        rec = _rec("s1", origin=None)
        rec.status = "archived"
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_merged_pr())
        await orch.recheck_stuck_complete_session("s1")
        await _drain()
        assert archived == [] and nudged == []
    asyncio.run(run())


def test_recheck_stuck_complete_session_archives_immediately_on_stale_ownership():
    """The ws.py StaleOwnership path calls this directly (not via the periodic sweep)
    the moment a resume attempt discovers the tracked item resolved — it must archive
    right away without waiting for the nudge cap, since this is direct live evidence,
    not a guess."""
    async def run():
        rec = _rec("s1", origin=None)
        rec.status = "complete"
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_merged_pr())
        await orch.recheck_stuck_complete_session("s1")
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
    asyncio.run(run())


# --------------------------------------------------------- open-question hold
#
# A completed session whose final hand-back still awaits the user (prose question
# next to the marker, or a structured question/permission that raced the archive
# task) must NOT archive: it flips back to waiting_question ("Answer needed") and
# consumes no completion nudge. Prod 2026-08-19: two of the four most recent
# completion archives ended "Want me to …?" + marker.

def _orch_question(records, pr_info, *, awaits, monkeypatch):
    """_orch plus the surfaces the open-question gate touches: a transcript, a
    status-persist spy, and a deterministic classifier verdict (*awaits*)."""
    orch, archived, nudged = _orch(records, pr_info)
    orch._drivers = {}
    orch.transcript_path = lambda sid: "/nonexistent/t.jsonl"  # type: ignore[method-assign]

    statuses: list[tuple[str, str]] = []

    async def _spy_persist(session_id, status):
        statuses.append((session_id, status))

    orch._persist_status = _spy_persist  # type: ignore[method-assign]

    import server.sessions.open_question as oq_mod
    import server.sessions.background_tasks as bg_mod

    async def _stub_awaits(text, **kw):
        return awaits

    monkeypatch.setattr(oq_mod, "completion_awaits_user", _stub_awaits)
    monkeypatch.setattr(bg_mod, "last_assistant_text",
                        lambda path: "Want me to do X next?")
    return orch, archived, nudged, statuses


def test_complete_with_open_prose_question_holds_as_handed_back(monkeypatch):
    """Armed PR (bar met, would archive) + open PROSE question → no archive, no
    nudge; the session is handed back to the user as `handed_back` (#2321 — the ask
    is in the closing prose, so there is no card and it is not waiting_question)."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=True, monkeypatch=monkeypatch)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "an open question must block the archive"
        assert nudged == [], "a question hold is not a not-ready nudge"
        assert statuses == [("s1", "handed_back")]
        assert "s1" not in orch._auto_archiving, "a later complete must be able to retry"
    asyncio.run(run())


def test_complete_without_question_archives(monkeypatch):
    """Classifier says the final message is a clean completion → archive as before."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=False, monkeypatch=monkeypatch)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
        assert statuses == []
    asyncio.run(run())


def _pending_driver(*, question=False, permission=False, plan=False):
    """A driver reporting exactly one structured card still open at archive time."""
    from server.sessions.status import StatusSignals
    return types.SimpleNamespace(_dash_signals=lambda: StatusSignals(
        turn_active=False, pending_permission=permission, pending_question=question,
        pending_plan=plan, turns_completed=1, last_turn_outcome="complete"))


def _no_classifier(monkeypatch):
    import server.sessions.open_question as oq_mod

    async def _boom(text, **kw):
        raise AssertionError("classifier must not run when a structured card "
                             "is already pending")

    monkeypatch.setattr(oq_mod, "completion_awaits_user", _boom)


def test_structured_pending_question_holds_without_classifier(monkeypatch):
    """A still-pending structured question (race: registered after the complete
    status persisted) holds the archive without any LLM call."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=False, monkeypatch=monkeypatch)
        orch._drivers = {"s1": _pending_driver(question=True)}
        _no_classifier(monkeypatch)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert statuses == [("s1", "waiting_question")]
    asyncio.run(run())


# The hold must hand the session back at the status that names the card actually
# open. Collapsing all three into waiting_question put "Answer needed" on sessions
# whose only open card was a permission prompt or a plan — a needs-you pill whose
# promised question is nowhere in the transcript.

def test_structured_pending_permission_holds_as_waiting_input(monkeypatch):
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=False, monkeypatch=monkeypatch)
        orch._drivers = {"s1": _pending_driver(permission=True)}
        _no_classifier(monkeypatch)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert statuses == [("s1", "waiting_input")]
    asyncio.run(run())


def test_structured_pending_plan_holds_as_ready_review(monkeypatch):
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=False, monkeypatch=monkeypatch)
        orch._drivers = {"s1": _pending_driver(plan=True)}
        _no_classifier(monkeypatch)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert statuses == [("s1", "ready_review")]
    asyncio.run(run())


def test_question_hold_does_not_consume_nudge_cap(monkeypatch):
    """Repeated question holds never eat into COMPLETE_NUDGE_CAP — the cap bounds
    not-ready nudges, and a held session did nothing wrong."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=True, monkeypatch=monkeypatch)
        for _ in range(3):
            orch._maybe_autoarchive_completed("s1", "complete", rec)
            await _drain()
        assert not getattr(orch, "_complete_nudge_counts", None), (
            "question holds must not increment the nudge counter")
        assert archived == [] and nudged == []
    asyncio.run(run())


# --------------------------------------------------------- Codex prose fallback
#
# `transcript_path` only ever resolves for Claude (Codex keeps no JSONL), and Codex's
# `pending_question` is structurally always False (no AskUserQuestion equivalent) — so
# without a turn-buffer fallback, a Codex session's prose question next to the
# completion marker had NO safeguard at all (#1283).

def _codex_driver(events):
    from server.sessions.status import StatusSignals
    return types.SimpleNamespace(
        _dash_signals=lambda: StatusSignals(
            turn_active=False, pending_permission=False, pending_question=False,
            pending_plan=False, turns_completed=1, last_turn_outcome="complete"),
        _turn_buffer=events)


def test_codex_prose_question_holds_via_turn_buffer(monkeypatch):
    """A Codex driver has no transcript and always reports pending_question=False;
    the fallback must still find the open question in its in-memory turn buffer."""
    import server.sessions.open_question as oq_mod
    from server.sessions import events as ev

    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, _armed_pr())
        orch.transcript_path = lambda sid: None  # type: ignore[method-assign]
        orch._drivers = {"s1": _codex_driver([
            ev.UserMessage(turn_id="t1", text="do the thing"),
            ev.TextDelta(turn_id="t1", text="Done. Want me to also do Y next? "
                                            "<!-- ccswitch:complete -->"),
        ])}

        async def _stub_awaits(text, **kw):
            assert "Want me to also do Y next?" in text
            return True

        monkeypatch.setattr(oq_mod, "completion_awaits_user", _stub_awaits)

        statuses: list[tuple[str, str]] = []

        async def _spy_persist(session_id, status):
            statuses.append((session_id, status))

        orch._persist_status = _spy_persist  # type: ignore[method-assign]
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "an open Codex question must block the archive"
        assert statuses == [("s1", "handed_back")]
    asyncio.run(run())


def test_codex_clean_completion_archives_via_turn_buffer(monkeypatch):
    """Ordinary Codex completion prose (no question) still archives — the fallback
    must not manufacture a hold where there is none."""
    import server.sessions.open_question as oq_mod
    from server.sessions import events as ev

    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, _armed_pr())
        orch.transcript_path = lambda sid: None  # type: ignore[method-assign]
        orch._drivers = {"s1": _codex_driver([
            ev.UserMessage(turn_id="t1", text="do the thing"),
            ev.TextDelta(turn_id="t1", text="Done. Let me know if anything looks off. "
                                            "<!-- ccswitch:complete -->"),
        ])}

        async def _stub_awaits(text, **kw):
            return False

        monkeypatch.setattr(oq_mod, "completion_awaits_user", _stub_awaits)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
    asyncio.run(run())


def test_global_disable_blocks_autoarchive():
    """When the app-wide auto-archive toggle is OFF, a completed session with an armed
    PR (bar met, would normally archive) is left untouched — no archive, no nudge, no
    status flip — exactly like a per-session manual opt-out. The guard is cleared so a
    later re-enable + `complete` can retry."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())

        async def _disabled() -> bool:
            return False

        orch._read_auto_archive_enabled = _disabled  # type: ignore[method-assign]
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "global disable must block the completion archive"
        assert nudged == [], "global disable is not a not-ready nudge"
        assert "s1" not in orch._auto_archiving, "guard cleared so a later re-enable can retry"
    asyncio.run(run())


def test_bare_orchestrator_without_gate_surfaces_still_archives():
    """A stub orchestrator missing _drivers/transcript surfaces (and any runtime
    failure inside the gate) degrades to 'no question' — archive proceeds."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
    asyncio.run(run())


# --------------------------------------------------------- claim verification
#
# Prod 2026-09-06 (session fdc6b27a, cairnea/KinDash, goal "Ship PR adding persistent
# color field per family member …"): the agent researched, wrote no code, recommended
# a spec, offered to draft it, and ended with `<!-- ccswitch:no-pr -->`. The card read
# "Complete" for the next eight minutes. Two holes: (1) the app-wide auto-archive
# toggle was OFF, and the toggle gated the WHOLE readiness gate (question hold, bar,
# classifier salvage, nudge), not just the archive; (2) even with it on, the marker's
# own `not-needed` token satisfied the bar with high confidence.

_CODE_GOAL = ("Ship PR adding persistent color field per family member with schema "
              "migration and UI controls.")


def _code_goal_rec(sid="s1", *, resolution=None, source=None):
    rec = _rec(sid, origin=None)
    rec.inferred_goal = _CODE_GOAL
    rec.pr_resolution = resolution
    rec.pr_resolution_source = source
    rec.status = "complete"
    return rec


def _stub_intent(monkeypatch, verdict):
    """Pin the PR-intent salvage classifier (never the real model in tests)."""
    calls: list[str] = []

    async def fake_classify(tail, goal, **kw):
        calls.append(goal)
        return verdict

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)
    return calls


async def _settle(pred, timeout: float = 5.0) -> None:
    """Wait until *pred()* holds instead of counting loop ticks. The claim-verification
    path awaits more than ``_drain``'s fixed budget — ``_autopilot_resolve_pr_intent``
    persists a confirmed verdict through ``db_session()``, a real round-trip whenever
    another test file in the same xdist worker already initialised a DB — so a
    tick-counting wait raced it (one local failure on 2026-09-06; green in CI)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("completion gate did not settle in time")
        await asyncio.sleep(0.005)


def test_global_disable_still_nudges_unmet_bar():
    """Auto-archive OFF gates the archive step ONLY: a completed session that is not
    actually done (open-unarmed PR) is still nudged toward the remaining work."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())

        async def _disabled() -> bool:
            return False

        orch._read_auto_archive_enabled = _disabled  # type: ignore[method-assign]
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "global disable must block the completion archive"
        assert nudged and "99" in nudged[0], "the readiness gate must still nudge"
    asyncio.run(run())


def test_global_disable_still_holds_open_question(monkeypatch):
    """Auto-archive OFF must not bury a hand-back: a completion message that still
    awaits the user flips to handed_back exactly as it does with archive on."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=True, monkeypatch=monkeypatch)

        async def _disabled() -> bool:
            return False

        orch._read_auto_archive_enabled = _disabled  # type: ignore[method-assign]
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [] and nudged == []
        assert statuses == [("s1", "handed_back")]
    asyncio.run(run())


def test_self_declared_no_pr_on_code_goal_is_nudged_when_unverified(monkeypatch):
    """A marker-sourced `not-needed` on a repo-attached code goal with zero PRs is a
    claim: the salvage classifier judges it against the goal, and when it does NOT
    confirm, the session is nudged (finish the work or ask the user) — not archived."""
    from server.automations.pr_intent import PrIntentVerdict

    async def run():
        rec = _code_goal_rec(resolution="not-needed", source="marker")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        calls = _stub_intent(monkeypatch, PrIntentVerdict(False, "", "high"))
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _settle(lambda: bool(nudged or archived))
        assert calls == [_CODE_GOAL], "the claim must be verified by the classifier"
        assert archived == [], "an unverified self-declared no-PR is not the finished state"
        assert nudged and "ask" in nudged[0].lower()
        assert rec.pr_resolution == "not-needed" and rec.pr_resolution_source == "marker"
    asyncio.run(run())


def test_self_declared_no_pr_on_code_goal_archives_when_classifier_confirms(monkeypatch):
    """When the classifier confirms the goal genuinely needed no code change, the
    resolution is upgraded to a verified one and the session archives."""
    from server.automations.pr_intent import PrIntentVerdict

    async def run():
        rec = _code_goal_rec(resolution="not-needed", source="marker")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        _stub_intent(monkeypatch, PrIntentVerdict(True, "not-needed", "high"))
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _settle(lambda: bool(archived or nudged))
        assert [a["session_id"] for a in archived] == ["s1"]
        assert nudged == []
        assert rec.pr_resolution_source == "classifier"
    asyncio.run(run())


# A pm-onboard map-diff session that answers `mark_no_pr` (not-needed) — the cluster
# is already covered by a doc under another slug — lands on source "tool", a
# self-claim on a repo-attached goal ("pm map-diff <slug>" reads as code work). Before
# the observation hook, the only way out was the Haiku salvage confirming; when it
# did not, the guard nudged "open a PR", the agent handed back, and a maintenance
# session sat in Active on a needs-you pill (prod 2026-09-18, session 91bf96b6).

def _map_diff_rec(sid="s1", *, source="tool"):
    rec = _rec(sid, origin="pm-onboard")
    rec.name = "pm map-diff viberpit"
    rec.origin_ref = "viberpit:map-diff:offline-mode"
    rec.pr_resolution = "not-needed"
    rec.pr_resolution_source = source
    rec.status = "complete"
    return rec


def test_map_diff_no_pr_archives_when_the_harness_observes_the_cluster_is_covered(monkeypatch):
    """The harness checked the repository and the cluster IS covered: the resolution
    is upgraded to the observed source, the classifier is never consulted, and the
    session archives without a nudge."""
    from server.automations.completion_bar import OBSERVED_RESOLUTION_SOURCE
    from server.automations.pr_intent import PrIntentVerdict

    async def run():
        rec = _map_diff_rec()
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        calls = _stub_intent(monkeypatch, PrIntentVerdict(False, "", "high"))
        observed: list[str] = []

        async def _observe(r):
            observed.append(r.id)
            return True

        orch._pm_onboard_observe_map_diff_not_needed = _observe  # type: ignore[method-assign]
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _settle(lambda: bool(archived or nudged))
        assert observed == ["s1"]
        assert [a["session_id"] for a in archived] == ["s1"]
        assert nudged == [], "an observed no-PR is the finished state — never nudge"
        assert calls == [], "the repository answered; the classifier is not consulted"
        assert rec.pr_resolution == "not-needed"
        assert rec.pr_resolution_source == OBSERVED_RESOLUTION_SOURCE
    asyncio.run(run())


def test_map_diff_no_pr_falls_through_to_the_classifier_when_unobserved(monkeypatch):
    """Observation could not confirm (no clone, unreadable ref, items uncited): the
    claim is judged exactly as before — the salvage classifier, then the nudge."""
    from server.automations.pr_intent import PrIntentVerdict

    async def run():
        rec = _map_diff_rec()
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        calls = _stub_intent(monkeypatch, PrIntentVerdict(False, "", "high"))

        async def _observe(r):
            return False

        orch._pm_onboard_observe_map_diff_not_needed = _observe  # type: ignore[method-assign]
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _settle(lambda: bool(archived or nudged))
        assert calls == ["pm map-diff viberpit"]
        assert archived == []
        assert nudged and "ask" in nudged[0].lower()
        assert rec.pr_resolution_source == "tool"
    asyncio.run(run())


def test_non_map_diff_self_claim_is_never_observed(monkeypatch):
    """The observation hook is a map-diff seam: a human session's marker-sourced
    no-PR goes straight to the classifier as before."""
    from server.automations.pr_intent import PrIntentVerdict

    async def run():
        rec = _code_goal_rec(resolution="not-needed", source="marker")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        calls = _stub_intent(monkeypatch, PrIntentVerdict(True, "not-needed", "high"))
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _settle(lambda: bool(archived or nudged))
        assert calls == [_CODE_GOAL]
        assert rec.pr_resolution_source == "classifier"
    asyncio.run(run())
