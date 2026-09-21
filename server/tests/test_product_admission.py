"""Read-only admission evidence must not turn unavailable telemetry into capacity."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from server.automations import product_admission as admission
from server.pm.repo_project import RepoOwner, RepoOwnership


def coverage():
    owner = RepoOwner("p", "product")
    return admission.ProductCoverage(
        {"org/repo": RepoOwnership("org/repo", "org/repo", "unique", owner, (owner,), (), "unique")},
        {"product": {"dispatch": "auto"}}, datetime.now(timezone.utc).isoformat())


def test_unknown_counts_are_not_zero_and_manual_does_not_bypass_them():
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 5, "max_spawns_per_day": 5}, None, None)
    decision, _ = snapshot.decide("org/repo", trigger="manual")
    assert decision.state == "unknown"
    assert {c.code for c in decision.constraints} >= {
        "runner_ceiling_unknown", "product_capacity_unknown", "daily_budget_unknown"}


def test_admitted_token_is_not_charged_twice_but_other_token_is():
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 5, "max_spawns_per_day": 1},
        {"org/repo#1": {"product": "product", "ownership_token": "token"}}, {"token": datetime.now(timezone.utc)})
    assert snapshot.decide("org/repo", existing_slot=True, existing_token="token")[0].permitted
    assert not snapshot.decide("org/repo", existing_slot=True, existing_token="other")[0].permitted


def test_dispatch_effect_comes_from_current_policy():
    evidence = coverage()
    evidence.policies["product"] = {"dispatch": "propose"}
    snapshot = admission.ImplementationSnapshot(
        evidence, "on", {"max_concurrent": 5, "max_spawns_per_day": 5}, {}, {})
    assert snapshot.decide("org/repo", effect="dispatch")[0].permitted
    assert not snapshot.decide("org/repo", effect="implementation")[0].permitted
    assert not snapshot.decide("org/repo", requested_product="other", effect="dispatch")[0].permitted


@pytest.mark.asyncio
async def test_runtime_failures_stay_unknown(monkeypatch):
    from server.automations import runner_config
    monkeypatch.setattr(admission, "gather_product_coverage", AsyncMock(return_value=coverage()))
    monkeypatch.setattr(runner_config, "read_automation_config", AsyncMock(return_value=(
        "auto", {"max_concurrent": 5, "max_spawns_per_day": 5})))
    monkeypatch.setattr(admission, "read_dispatch_accounting", AsyncMock(side_effect=ValueError("bad")))
    context = AsyncMock()
    context.list_target_states.side_effect = RuntimeError("unavailable")
    snapshot = await admission.gather_implementation_snapshot(["org/repo"], context=context)
    assert snapshot.runner_mode == "on"
    assert snapshot.decide("org/repo")[0].state == "unknown"


def test_existing_slot_must_belong_to_the_current_product_and_token():
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 1, "max_spawns_per_day": 5},
        {"org/repo#1": {"product": "other", "ownership_token": "token"}}, {})
    assert not snapshot.decide("org/repo", existing_slot=True, existing_token="token")[0].permitted


def test_pr_doctor_pending_product_refuses_a_new_claim_only():
    # A real matching state row is required for `existing_slot=True` to verify
    # as `occupied` (the same D8 carve-out every other capacity check uses,
    # test_existing_slot_must_belong_to_the_current_product_and_token above
    # guards it can't be spoofed by the flag alone) — an empty `states` can
    # never produce a "held" claim.
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 5, "max_spawns_per_day": 5},
        {"org/repo#1": {"product": "product", "ownership_token": "t"}}, {},
        frozenset({"product"}))
    new_claim, _ = snapshot.decide("org/repo")
    assert not new_claim.permitted
    assert any(c.code == "pr_doctor_priority" for c in new_claim.constraints)

    held, _ = snapshot.decide("org/repo", existing_slot=True, existing_token="t")
    codes = {c.code for c in held.constraints}
    assert "pr_doctor_priority" not in codes
    assert "pr_doctor_priority_held" in codes


def test_pr_doctor_pending_product_absent_by_default():
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 5, "max_spawns_per_day": 5}, {}, {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code for c in decision.constraints}
    assert "pr_doctor_priority" not in codes and "pr_doctor_priority_held" not in codes


@pytest.mark.asyncio
async def test_gather_implementation_snapshot_reads_pr_doctor_pending(monkeypatch):
    from server.automations import runner_config
    monkeypatch.setattr(admission, "gather_product_coverage", AsyncMock(return_value=coverage()))
    monkeypatch.setattr(runner_config, "read_automation_config", AsyncMock(return_value=(
        "auto", {"max_concurrent": 5, "max_spawns_per_day": 5})))
    monkeypatch.setattr(admission, "read_dispatch_accounting", AsyncMock(return_value={}))
    context = AsyncMock()
    context.list_target_states.return_value = {}

    async def pending():
        return frozenset({"product"})

    snapshot = await admission.gather_implementation_snapshot(
        ["org/repo"], context=context, pr_doctor_pending=pending)

    assert snapshot.pr_doctor_pending == frozenset({"product"})


@pytest.mark.asyncio
async def test_gather_implementation_snapshot_pr_doctor_pending_failure_is_empty(monkeypatch):
    from server.automations import runner_config
    monkeypatch.setattr(admission, "gather_product_coverage", AsyncMock(return_value=coverage()))
    monkeypatch.setattr(runner_config, "read_automation_config", AsyncMock(return_value=(
        "auto", {"max_concurrent": 5, "max_spawns_per_day": 5})))
    monkeypatch.setattr(admission, "read_dispatch_accounting", AsyncMock(return_value={}))
    context = AsyncMock()
    context.list_target_states.return_value = {}

    async def broken():
        raise RuntimeError("pr doctor read failed")

    snapshot = await admission.gather_implementation_snapshot(
        ["org/repo"], context=context, pr_doctor_pending=broken)

    assert snapshot.pr_doctor_pending == frozenset()


@pytest.mark.asyncio
async def test_mixed_repository_batch_cannot_inherit_first_product(monkeypatch):
    evidence = coverage()
    owner = RepoOwner("q", "second")
    evidence.ownership["org/other"] = RepoOwnership(
        "org/other", "org/other", "unique", owner, (owner,), (), "unique")
    snapshot = admission.ImplementationSnapshot(
        evidence, "on", {"max_concurrent": 5, "max_spawns_per_day": 5}, {}, {})
    gather = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(admission, "gather_implementation_snapshot", gather)
    decision, _ = await admission.evaluate_implementation(
        "org/repo", target_refs=["org/repo#1", "org/other#2"])
    assert not decision.permitted
    assert "targets_mismatch" in {c.code for c in decision.constraints}
    assert gather.await_args.args[0] == {"org/repo", "org/other"}


def _coverage(policy: dict) -> admission.ProductCoverage:
    owner = RepoOwner("p", "product")
    return admission.ProductCoverage(
        {"org/repo": RepoOwnership("org/repo", "org/repo", "unique", owner, (owner,), (), "unique")},
        {"product": policy}, datetime.now(timezone.utc).isoformat())


def _states(n_open: int, *, product: str = "product", handed_off: bool = False) -> dict:
    """*n_open* claims for *product* on org/repo; with ``handed_off`` each has freed
    its SLOT (PR Doctor carries the PR) but still counts toward the pipeline."""
    return {f"org/repo#{i}": {"claimed": True, "repo": "org/repo", "product": product,
                              "ownership_token": f"t{i}", "session_id": f"s{i}",
                              **({"handed_off": True} if handed_off else {})}
            for i in range(1, n_open + 1)}


def test_pipeline_full_refuses_a_new_claim_only(monkeypatch):
    """Spec 2026-09-20 §9: pipeline >= cap refuses a NEW claim with
    product_pipeline_full; an existing_slot re-validation is never refused on it."""
    cov = _coverage({"dispatch": "auto", "max_in_flight": 9, "max_pipeline_prs": 2})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(2), {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code: c for c in decision.constraints}
    assert not decision.permitted and codes["product_pipeline_full"].state == "refused"
    assert codes["product_pipeline_full"].text == "pipeline full: 2 claims and PRs open (cap 2)"
    held, _ = snapshot.decide("org/repo", existing_slot=True, existing_token="t1")
    held_codes = {c.code: c.state for c in held.constraints}
    assert "product_pipeline_full" not in held_codes and held_codes["product_pipeline_held"] == "permitted"


def test_blank_pipeline_cap_never_refuses():
    cov = _coverage({"dispatch": "auto", "max_in_flight": 9})   # max_pipeline_prs absent → None
    # handed_off=True: these free their slots (product_capacity/runner_ceiling stay
    # under the small caps above) but still count toward the pipeline — the point
    # is a large pipeline (50) with no cap set never refuses on THAT dimension.
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(50, handed_off=True), {})
    decision, _ = snapshot.decide("org/repo")
    assert not any(c.code.startswith("product_pipeline") for c in decision.constraints)
    assert decision.permitted


def test_handed_off_prs_count_toward_the_pipeline_regardless_of_pr_doctor():
    """C23: the pin frees a slot at hand-off when PR Doctor covers the repo; the
    pipeline cap does not — a handed-off PR is still open and unmerged."""
    cov = _coverage({"dispatch": "auto", "max_in_flight": 9, "max_pipeline_prs": 2})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(2, handed_off=True), {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code: c.state for c in decision.constraints}
    assert codes["product_pipeline_full"] == "refused"
    assert codes["product_capacity_available"] == "permitted"     # the slots ARE free


def test_pipeline_count_unknown_when_states_are_unreadable():
    cov = _coverage({"dispatch": "auto", "max_pipeline_prs": 2})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                None, {})
    decision, _ = snapshot.decide("org/repo")
    assert {c.code for c in decision.constraints} >= {"product_pipeline_unknown"}


def test_pipeline_cap_zero_refuses_every_new_claim():
    """A blank ``max_pipeline_prs`` is never (checked above); ``0`` is a real cap
    that refuses every new claim on an otherwise-empty pipeline — pins that
    blank != 0 (carried item C, spec 2026-09-20 D13)."""
    cov = _coverage({"dispatch": "auto", "max_in_flight": 9, "max_pipeline_prs": 0})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                {}, {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code: c for c in decision.constraints}
    assert not decision.permitted and codes["product_pipeline_full"].state == "refused"
    assert codes["product_pipeline_full"].text == "pipeline full: 0 claims and PRs open (cap 0)"


def test_handed_off_claim_is_never_refused_by_its_own_pipeline_slot():
    """Governor-spec D8: a handed-off claim's PR is still open and counts toward
    the pipeline (``counts_toward_pipeline``), but ``is_in_flight`` is False for it
    (PR Doctor now carries it) — so the pipeline's own occupancy check must not be
    keyed off ``is_in_flight`` (which produced the ``occupied`` flag the other three
    ceilings use), or a handed-off row would be refused by the very count it
    contributes to. A NEW claim on the same, still-full pipeline is still refused."""
    cov = _coverage({"dispatch": "auto", "max_in_flight": 9, "max_pipeline_prs": 1})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(1, handed_off=True), {})
    decision, _ = snapshot.decide("org/repo", existing_slot=True, existing_token="t1")
    codes = {c.code: c.state for c in decision.constraints}
    assert codes["product_pipeline_held"] == "permitted"
    assert "product_pipeline_full" not in codes

    new_claim, _ = snapshot.decide("org/repo")
    new_codes = {c.code: c.state for c in new_claim.constraints}
    assert new_codes["product_pipeline_full"] == "refused"
