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
