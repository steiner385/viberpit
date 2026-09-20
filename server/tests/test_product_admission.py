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
