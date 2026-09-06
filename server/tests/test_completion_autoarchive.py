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

    async def _spy_send_prompt(session_id, prompt):
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
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/42")


def _unarmed_pr():
    return types.SimpleNamespace(
        number=99, state="open", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/99")


def _merged_pr():
    return types.SimpleNamespace(
        number=7, state="merged", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/7")


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


def test_complete_with_open_question_holds_as_waiting_question(monkeypatch):
    """Armed PR (bar met, would archive) + open prose question → no archive, no
    nudge; the session is handed back to the user as waiting_question."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=True, monkeypatch=monkeypatch)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "an open question must block the archive"
        assert nudged == [], "a question hold is not a not-ready nudge"
        assert statuses == [("s1", "waiting_question")]
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


def test_structured_pending_question_holds_without_classifier(monkeypatch):
    """A still-pending structured question/permission (race: registered after the
    complete status persisted) holds the archive without any LLM call."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged, statuses = _orch_question(
            {"s1": rec}, _armed_pr(), awaits=False, monkeypatch=monkeypatch)

        from server.sessions.status import StatusSignals
        driver = types.SimpleNamespace(_dash_signals=lambda: StatusSignals(
            turn_active=False, pending_permission=False, pending_question=True,
            pending_plan=False, turns_completed=1, last_turn_outcome="complete"))
        orch._drivers = {"s1": driver}

        import server.sessions.open_question as oq_mod

        async def _boom(text, **kw):
            raise AssertionError("classifier must not run when a structured "
                                 "question is already pending")

        monkeypatch.setattr(oq_mod, "completion_awaits_user", _boom)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert statuses == [("s1", "waiting_question")]
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
        assert statuses == [("s1", "waiting_question")]
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
