"""``_pr_doctor_pending_products`` — product slugs PR Doctor is currently
gating Backlog Runner for (spec 2026-09-21 pr-doctor-priority).

Reuses the ``_build_orch``/file-DB pattern from ``test_pr_doctor_sweep.py``.
"""
from __future__ import annotations

import asyncio

from dataclasses import replace

from server.tests.test_pr_doctor_sweep import (
    _build_orch, _init_pr_doctor_db, _pr, _set_config, _set_mode)


async def _register_repo(repo: str, slug: str) -> None:
    from server.db import db_session
    from server.db.models import Project, ProjectRepo

    async with db_session() as db:
        project = Project(slug=slug, name=slug)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo))


async def _init_db(tmp_path) -> None:
    await _init_pr_doctor_db(tmp_path)


def _stuck(number=7):
    return _pr(number=number, checks="failing", merge_state_status="BLOCKED")


def test_a_stuck_pr_pr_doctor_will_fix_gates_its_product(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_fix_failing_ci": True})
        await _register_repo("o/r", "product-a")

        stuck = _pr(checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_gave_up_target_does_not_gate(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_fix_failing_ci": True})
        await _register_repo("o/r", "product-a")

        stuck = _pr(number=9, checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])
        await orch._pr_doctor_ctx.set_target_state("o/r#9", {"gave_up": True})

        pending = await orch._pr_doctor_pending_products()

        assert pending == {}

    asyncio.run(run())


def test_a_healthy_pr_does_not_gate(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_fix_failing_ci": True})
        await _register_repo("o/r", "product-a")

        healthy = _pr(checks="passing", merge_state_status="CLEAN", mergeable="MERGEABLE")
        orch = _build_orch(cache={("o/r", "feat/x"): healthy}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == {}

    asyncio.run(run())


def _gate_case(name, mode, config, pr, state=None, expect=None):
    def test(monkeypatch, tmp_path):
        async def run():
            await _init_db(tmp_path)
            await _set_mode(monkeypatch, mode)
            await _set_config(config)
            await _register_repo("o/r", "product-a")
            orch = _build_orch(cache={("o/r", "feat/x"): pr}, pairs=[("o/r", "feat/x")])
            if state:
                await orch._pr_doctor_ctx.set_target_state(f"o/r#{pr.number}", state)
            assert await orch._pr_doctor_pending_products() == (expect or {})

        asyncio.run(run())
    test.__name__ = name
    return test


_ON = {"auto_fix_failing_ci": True}
test_mode_off_does_not_gate = _gate_case(
    "test_mode_off_does_not_gate", "off", _ON, _stuck())
test_mode_propose_does_not_gate = _gate_case(
    "test_mode_propose_does_not_gate", "propose", _ON, _stuck())
test_auto_flag_off_does_not_gate = _gate_case(
    "test_auto_flag_off_does_not_gate", "auto", {"auto_fix_failing_ci": False}, _stuck())
test_blocked_review_gate_does_not_gate = _gate_case(
    "test_blocked_review_gate_does_not_gate", "auto", _ON,
    _pr(checks="passing", merge_state_status="BLOCKED"))
test_attempts_at_ceiling_does_not_gate = _gate_case(
    "test_attempts_at_ceiling_does_not_gate", "auto", {**_ON, "max_attempts": 3},
    _stuck(), state={"total_attempts": 3})
test_auto_with_flag_on_gates = _gate_case(
    "test_auto_with_flag_on_gates", "auto", _ON, _stuck(), expect={"product-a": ("o/r#7",)})


def test_stale_per_branch_cache_does_not_gate_when_population_says_merged(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config(_ON)
        await _register_repo("o/r", "product-a")
        stale_open = replace(_stuck(number=578), head_ref="feat/x")
        merged_pop = replace(stale_open, state="merged", checks="none", merge_state_status="")
        orch = _build_orch(cache={("o/r", "feat/x"): stale_open}, pairs=[("o/r", "feat/x")],
                           records={}, population={"o/r": (merged_pop,)})

        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# #2202: a PR PR Doctor cannot CLAIM must not reserve the product's capacity
# ---------------------------------------------------------------------------

_HEAD = "a" * 40  # _pr()'s default head_sha
_CONFLICT = "target already has an operation"


async def _record_refusal(orch, target, head_sha, reason=_CONFLICT):
    """What ``_pr_doctor_refusal_detail`` writes on a refused claim."""
    await orch._pr_doctor_ctx.emit(
        "warn", reason, kind="ownership_wait", target=target,
        detail={"claim_refused": True, "head_sha": head_sha, "reason": reason})


async def _gated_orch(monkeypatch, tmp_path, *, config=None, state=None):
    await _init_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _set_config(config or _ON)
    await _register_repo("o/r", "product-a")
    orch = _build_orch(cache={("o/r", "feat/x"): _stuck()}, pairs=[("o/r", "feat/x")])
    if state:
        await orch._pr_doctor_ctx.set_target_state("o/r#7", state)
    return orch


def test_a_claim_refused_at_the_current_head_does_not_gate(monkeypatch, tmp_path):
    """The #2202 deadlock: PR Doctor decides to fix the PR but every claim is
    refused by a foreign owner, so reserving the product starves it forever."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        await _record_refusal(orch, "o/r#7", _HEAD)

        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


def test_a_claim_refused_at_an_older_head_still_gates(monkeypatch, tmp_path):
    """A push re-arms the target and PR Doctor tries again, so a refusal
    recorded against a superseded head proves nothing about this one."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        await _record_refusal(orch, "o/r#7", "b" * 40)

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_refusal_under_pr_doctors_own_ownership_still_gates(monkeypatch, tmp_path):
    """A refused TRANSFER (``prior PR execution has not stopped``) is PR
    Doctor's own recovery lifecycle waiting for its predecessor to stop — it
    clears itself, so the reservation is real."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path, state={"ownership_token": "tok-1"})
        await _record_refusal(orch, "o/r#7", _HEAD, "prior PR execution has not stopped")

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_an_attempt_at_this_head_supersedes_an_earlier_refusal(monkeypatch, tmp_path):
    """The blocker cleared and PR Doctor claimed: a repair really is running at
    this head, so the stale refusal must not un-gate the product."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path, state={"last_action_sha": _HEAD})
        await _record_refusal(orch, "o/r#7", _HEAD)

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_newer_live_session_wait_supersedes_the_refusal(monkeypatch, tmp_path):
    """``_pr_doctor_session_fix_owned`` emits its attached-live-session
    ``ownership_wait`` only AFTER a successful claim, and carries no
    ``claim_refused`` marker — the newest word per target wins."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        await _record_refusal(orch, "o/r#7", _HEAD)
        await orch._pr_doctor_ctx.emit(
            "warn", "o/r#7 is still attached to session sess1", kind="ownership_wait",
            target="o/r#7", session_id=None)

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2026-09-30 audit, D6: a fix already injected at this head is not "pending"
# ---------------------------------------------------------------------------

def test_a_session_fix_already_injected_at_this_head_does_not_gate(monkeypatch, tmp_path):
    """The sweep's per-head one-shot guard (``engine.acted_at_head``) means PR
    Doctor will not touch this PR again until a push — so reserving the product
    for it holds capacity for a repair that is not coming (KinDash#17383)."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path, state=_fixed_at(_HEAD))

        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


def _fixed_at(head, *, kind="failing_ci"):
    """The row ``_pr_doctor_session_fix`` leaves after injecting a fix at *head*:
    ``apply_rearm`` stamps ``last_head_sha``/``last_failure`` and ``record_attempt``
    consumes the attempt and stamps ``last_action_sha``."""
    return {"last_head_sha": head, "last_failure": kind, "last_action_sha": head,
            "attempts": 1, "total_attempts": 1, "attempted": True}


def test_a_branch_update_already_done_at_this_head_does_not_gate(monkeypatch, tmp_path):
    """Same guard, mechanical kind: the update-branch PR Doctor ran on
    KinDash#17382 at 11:34:02 UTC kept the product gated at 11:34:57."""
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_update_out_of_date": True})
        await _register_repo("o/r", "product-a")
        behind = _pr(checks="passing", merge_state_status="BEHIND")
        orch = _build_orch(cache={("o/r", "feat/x"): behind}, pairs=[("o/r", "feat/x")])
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#7", {"last_head_sha": _HEAD, "last_failure": "out_of_date",
                      "last_action_sha": _HEAD, "mechanical_attempts": 1, "total_mechanical": 1})

        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


def test_a_repair_still_running_at_this_head_keeps_gating(monkeypatch, tmp_path):
    """An operation held under PR Doctor's own token IS the running repair —
    the per-head guard must not un-gate it (the ``_pr_doctor_claim_unclaimable``
    rule 2 reservation stays real while the session works)."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path, state={
            **_fixed_at(_HEAD), "ownership_token": "tok-1", "session_id": "sess-1"})

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_push_since_the_last_fix_re_arms_the_gate(monkeypatch, tmp_path):
    """``last_action_sha`` at an OLDER head proves nothing about this one: the
    sweep would act again, so the reservation is real."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path, state=_fixed_at("b" * 40))

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_only_the_unclaimable_product_is_dropped(monkeypatch, tmp_path):
    """Per-product, exactly like the gate itself: one product's unclaimable PR
    never un-gates another product's claimable one."""
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config(_ON)
        await _register_repo("o/r", "product-a")
        await _register_repo("o/other", "product-b")
        orch = _build_orch(
            cache={("o/r", "feat/x"): _stuck(), ("o/other", "feat/y"): _stuck(number=8)},
            pairs=[("o/r", "feat/x"), ("o/other", "feat/y")])
        await _record_refusal(orch, "o/r#7", _HEAD)

        assert await orch._pr_doctor_pending_products() == {"product-b": ("o/other#8",)}

    asyncio.run(run())


def test_every_gating_pr_of_a_product_is_named(monkeypatch, tmp_path):
    """The refs are what the admission refusal text and the Capacity page show,
    so all of a product's reserving PRs must be there, sorted."""
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config(_ON)
        await _register_repo("o/r", "product-a")
        orch = _build_orch(
            cache={("o/r", "feat/x"): _stuck(), ("o/r", "feat/y"): _stuck(number=8)},
            pairs=[("o/r", "feat/x"), ("o/r", "feat/y")])

        assert await orch._pr_doctor_pending_products() == {
            "product-a": ("o/r#7", "o/r#8")}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The write side: which refusals earn the durable marker
# ---------------------------------------------------------------------------

def test_refusal_detail_marks_only_ownership_conflicts():
    from server.automations.ownership import OwnershipConflict, StaleOwnership
    from server.automations.target_identity import TargetIdentityError
    from server.sessions.orchestrator import SessionOrchestrator

    detail = SessionOrchestrator._pr_doctor_refusal_detail(
        OwnershipConflict(_CONFLICT), _stuck())
    assert detail == {"claim_refused": True, "head_sha": _HEAD, "reason": _CONFLICT}
    # A race the next sweep tick retries must NOT un-gate a whole head SHA.
    for exc in (StaleOwnership("PR state changed during acquisition"),
                TargetIdentityError("target identity unavailable"),
                ValueError("no mechanical evidence")):
        assert SessionOrchestrator._pr_doctor_refusal_detail(exc, _stuck()) is None


def test_a_refused_session_fix_records_the_marker_the_gate_reads(monkeypatch, tmp_path):
    """Closes the loop: the refusal ``_pr_doctor_session_fix`` actually emits is
    the evidence that un-gates the product, with no hand-written detail."""
    async def run():
        from server.automations.ownership import OwnershipConflict
        orch = await _gated_orch(monkeypatch, tmp_path)

        async def _refuse(*a, **kw):
            raise OwnershipConflict(_CONFLICT)

        orch._pr_doctor_claim = _refuse  # type: ignore[method-assign]
        status, _, reason = await orch._pr_doctor_session_fix(
            "o/r", "feat/x", _stuck(), "failing_ci", None)
        assert (status, reason) == ("transient", _CONFLICT)

        events = await orch._pr_doctor_ctx.list_events(kind="ownership_wait")
        assert events[0]["detail"] == {
            "claim_refused": True, "head_sha": _HEAD, "reason": _CONFLICT}
        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# #2732: PR Doctor's OWN sessionless operation stuck in ``uncertain``
# ---------------------------------------------------------------------------

def _op(*, age_s, phase="uncertain", session_id=None, owner="pr-doctor"):
    from types import SimpleNamespace
    from datetime import timedelta
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return SimpleNamespace(owner=owner, phase=phase, session_id=session_id,
                           reserved_session_id=None,
                           updated_at=now - timedelta(seconds=age_s))


def test_mechanical_stuck_needs_age_phase_and_no_session():
    from server.sessions import orchestrator as o
    stuck = o.SessionOrchestrator._pr_doctor_mechanical_stuck
    old = o._PR_DOCTOR_MECHANICAL_DEADLINE_S + 60
    assert stuck(_op(age_s=old))
    assert not stuck(_op(age_s=10))
    assert not stuck(_op(age_s=old, phase="claimed"))
    assert not stuck(_op(age_s=old, session_id="s1"))
    assert not stuck(_op(age_s=old, owner="backlog-runner"))
    assert not stuck(None)


def _own_stuck_state():
    return {"ownership_token": "tok-1", "mechanical_attempts": 1,
            "last_failure": "stuck_enqueue", "last_action_sha": _HEAD}


def test_own_operation_stuck_past_the_deadline_stops_gating(monkeypatch, tmp_path):
    async def run():
        from server.sessions import orchestrator as o
        orch = await _gated_orch(monkeypatch, tmp_path, state=_own_stuck_state())
        await _record_refusal(orch, "o/r#7", _HEAD, "prior PR execution has not stopped")

        async def op(_token):
            return _op(age_s=o._PR_DOCTOR_MECHANICAL_DEADLINE_S + 60)
        monkeypatch.setattr(orch, "_pr_doctor_claim_operation", op)

        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


def test_own_operation_within_the_deadline_keeps_gating(monkeypatch, tmp_path):
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path, state=_own_stuck_state())
        await _record_refusal(orch, "o/r#7", _HEAD, "prior PR execution has not stopped")

        async def op(_token):
            return _op(age_s=10)
        monkeypatch.setattr(orch, "_pr_doctor_claim_operation", op)

        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2026-10-08: a usage-stalled attached session must not reserve the product
# (KinDash#18420 held KinDash from 09:37 to 13:08 ET behind a Codex session
# paused on ``all_accounts_busy`` that could never stop or progress)
# ---------------------------------------------------------------------------

_STALLED = {"status": "needs_attention", "paused_reason": "all_accounts_busy"}


async def _attached_wait(orch, target="o/r#7", sid="sess1"):
    """What ``_pr_doctor_session_fix_owned`` emits when the target is still
    attached to a live session."""
    await orch._pr_doctor_ctx.emit(
        "warn", f"{target} is still attached to session {sid}", kind="ownership_wait",
        target=target, session_id=sid, detail={"attached_session": sid})


async def _waiting_orch(monkeypatch, tmp_path, *, status, paused_reason=None):
    orch = await _gated_orch(monkeypatch, tmp_path)
    rec = orch._records["sess1"]
    rec.status = status
    rec.paused_reason = paused_reason
    await _attached_wait(orch)
    return orch, rec


def test_a_wait_on_a_usage_stalled_attached_session_does_not_gate(monkeypatch, tmp_path, caplog):
    import logging

    async def run():
        orch, _ = await _waiting_orch(monkeypatch, tmp_path, **_STALLED)
        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            assert await orch._pr_doctor_pending_products() == {}
        assert any("pr_doctor_gate" in r.getMessage() and "o/r#7" in r.getMessage()
                   and "unclaimable=" in r.getMessage() for r in caplog.records)

    asyncio.run(run())


def test_a_wait_on_a_working_attached_session_keeps_gating(monkeypatch, tmp_path):
    async def run():
        orch, _ = await _waiting_orch(monkeypatch, tmp_path, status="in_progress")
        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_wait_on_a_session_paused_for_a_non_usage_reason_keeps_gating(monkeypatch, tmp_path):
    async def run():
        orch, _ = await _waiting_orch(monkeypatch, tmp_path, status="needs_attention",
                                      paused_reason="crash_cap")
        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_stalled_session_that_recovers_re_gates_with_no_sticky_state(monkeypatch, tmp_path):
    async def run():
        orch, rec = await _waiting_orch(monkeypatch, tmp_path, **_STALLED)
        assert await orch._pr_doctor_pending_products() == {}
        rec.status, rec.paused_reason = "in_progress", None
        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


async def _add_session_row(sid, *, status, paused_reason=None):
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        db.add(DbSession(id=sid, name="remote", account_name="acct-a", cwd="/tmp",
                         status=status, paused_reason=paused_reason))


def test_a_wait_on_a_usage_stalled_session_on_another_host_does_not_gate(monkeypatch, tmp_path):
    """A predecessor on another fleet host has no in-memory record here: its
    shared ``sessions`` row is the evidence."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        await _add_session_row("remote-1", **_STALLED)
        await _attached_wait(orch, sid="remote-1")
        assert await orch._pr_doctor_pending_products() == {}

    asyncio.run(run())


def test_a_wait_on_a_working_session_on_another_host_keeps_gating(monkeypatch, tmp_path):
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        await _add_session_row("remote-1", status="in_progress")
        await _attached_wait(orch, sid="remote-1")
        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_the_in_memory_record_wins_over_the_db_row(monkeypatch, tmp_path):
    """The local record is fresher on its own host than the shared row."""
    async def run():
        orch, _ = await _waiting_orch(monkeypatch, tmp_path, status="in_progress")
        await _add_session_row("sess1", **_STALLED)
        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_a_wait_on_a_session_unknown_to_memory_and_db_keeps_gating(monkeypatch, tmp_path):
    """Evidence, never inference: a session nobody knows proves no stall."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        await _attached_wait(orch, sid="ghost")
        assert await orch._pr_doctor_pending_products() == {"product-a": ("o/r#7",)}

    asyncio.run(run())


def test_db_fallback_is_one_batched_query(monkeypatch, tmp_path):
    """Admission hot path: every off-host predecessor is read in one query."""
    async def run():
        from sqlalchemy import event
        orch = await _gated_orch(monkeypatch, tmp_path)
        for sid in ("r1", "r2", "r3"):
            await _add_session_row(sid, **_STALLED)
        selects = []
        from server.db import db_session
        async with db_session() as db:
            sync_engine = db.get_bind()

        def _count(conn, cursor, statement, *a):
            if "FROM sessions" in statement and "paused_reason" in statement:
                selects.append(statement)

        event.listen(sync_engine, "before_cursor_execute", _count)
        try:
            state = await orch._pr_doctor_attached_stall_state(["r1", "r2", "r3", "ghost"])
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)
        assert set(state) == {"r1", "r2", "r3"}
        assert len(selects) == 1

    asyncio.run(run())


def test_the_attached_session_wait_names_a_usage_stall(monkeypatch, tmp_path):
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        rec = orch._records["sess1"]
        rec.status, rec.paused_reason = _STALLED["status"], _STALLED["paused_reason"]

        status, _, _ = await orch._pr_doctor_session_fix_owned(
            "o/r", "feat/x", _stuck(), "failing_ci", rec, ownership_token="tok-1")
        assert status == "transient"

        event = (await orch._pr_doctor_ctx.list_events(kind="ownership_wait"))[0]
        assert event["summary"] == (
            "o/r#7 is waiting on session sess1, which is stalled on usage "
            "(all_accounts_busy); hand its work to a session that can run, or archive it, "
            "to let recovery start")
        assert event["detail"]["attached_session"] == "sess1"
        # D8: the predecessor is left exactly where it was.
        assert "sess1" in orch._drivers and rec.status == "needs_attention"

    asyncio.run(run())


def test_the_attached_session_wait_keeps_its_text_for_a_working_session(monkeypatch, tmp_path):
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        rec = orch._records["sess1"]
        rec.status = "in_progress"

        await orch._pr_doctor_session_fix_owned(
            "o/r", "feat/x", _stuck(), "failing_ci", rec, ownership_token="tok-1")

        event = (await orch._pr_doctor_ctx.list_events(kind="ownership_wait"))[0]
        assert event["summary"] == (
            "o/r#7 is still attached to session sess1; stop its managed work before recovery")
        assert event["detail"]["attached_session"] == "sess1"

    asyncio.run(run())


def test_the_attached_session_wait_names_a_limited_predecessor_by_status(monkeypatch, tmp_path):
    """``limited`` is usage-stalled with no ``paused_reason`` — never "(None)"."""
    async def run():
        orch = await _gated_orch(monkeypatch, tmp_path)
        rec = orch._records["sess1"]
        rec.status, rec.paused_reason = "limited", None

        await orch._pr_doctor_session_fix_owned(
            "o/r", "feat/x", _stuck(), "failing_ci", rec, ownership_token="tok-1")

        event = (await orch._pr_doctor_ctx.list_events(kind="ownership_wait"))[0]
        assert "stalled on usage (limited);" in event["summary"]
        assert "None" not in event["summary"]

    asyncio.run(run())


async def _two_product_orch(monkeypatch, tmp_path):
    """product-a gates normally on o/r#7; product-b's o/other#8 waits on an
    off-host session row."""
    await _init_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _set_config(_ON)
    await _register_repo("o/r", "product-a")
    await _register_repo("o/other", "product-b")
    orch = _build_orch(
        cache={("o/r", "feat/x"): _stuck(), ("o/other", "feat/y"): _stuck(number=8)},
        pairs=[("o/r", "feat/x"), ("o/other", "feat/y")])
    return orch


def test_a_failed_stall_read_keeps_every_other_reservation(monkeypatch, tmp_path):
    """The stall read's DB failure is contained: it must not fall through to
    the blanket ``except`` that returns ``{}`` for EVERY product. The unread
    id keeps gating (evidence, never inference)."""
    async def run():
        import contextlib
        import server.sessions.orchestrator as orch_mod
        orch = await _two_product_orch(monkeypatch, tmp_path)
        await _add_session_row("remote-1", **_STALLED)
        await _attached_wait(orch, target="o/other#8", sid="remote-1")

        real = orch_mod.db_session

        class _FailStallRead:
            def __init__(self, db):
                self._db = db

            def __getattr__(self, name):
                return getattr(self._db, name)

            async def execute(self, stmt, *a, **kw):
                if "paused_reason" in str(stmt):
                    raise RuntimeError("db down")
                return await self._db.execute(stmt, *a, **kw)

        @contextlib.asynccontextmanager
        async def _db_session():
            async with real() as db:
                yield _FailStallRead(db)

        monkeypatch.setattr(orch_mod, "db_session", _db_session)

        assert await orch._pr_doctor_pending_products() == {
            "product-a": ("o/r#7",), "product-b": ("o/other#8",)}

    asyncio.run(run())


def test_the_gate_reads_events_once_and_stall_rows_at_most_once(monkeypatch, tmp_path):
    """Review Focus 4 through the real path: several attached targets, one
    ``ownership_wait`` read and at most one batched ``sessions`` select. An
    attached wait for a target outside the gate's scope is never resolved."""
    async def run():
        from sqlalchemy import event
        from server.db import db_session
        orch = await _two_product_orch(monkeypatch, tmp_path)
        for sid in ("remote-1", "remote-2", "remote-3"):
            await _add_session_row(sid, **_STALLED)
        await _attached_wait(orch, target="o/r#7", sid="remote-1")
        await _attached_wait(orch, target="o/other#8", sid="remote-2")
        await _attached_wait(orch, target="o/elsewhere#99", sid="remote-3")

        reads = []
        real_list = orch._pr_doctor_ctx.list_events

        async def _list_events(**kw):
            reads.append(kw.get("kind"))
            return await real_list(**kw)

        monkeypatch.setattr(orch._pr_doctor_ctx, "list_events", _list_events)

        selects = []
        async with db_session() as db:
            sync_engine = db.get_bind()

        def _count(conn, cursor, statement, params, *a):
            if "FROM sessions" in statement and "paused_reason" in statement:
                selects.append(params)

        event.listen(sync_engine, "before_cursor_execute", _count)
        try:
            assert await orch._pr_doctor_pending_products() == {}
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)
        assert reads == ["ownership_wait"]
        assert len(selects) == 1
        assert "remote-3" not in repr(selects[0])

    asyncio.run(run())
