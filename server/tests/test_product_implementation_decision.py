"""The policy preview and effect admission share a fail-closed composition."""
from dataclasses import replace

import pytest

from server.automations.product_policy import (
    ImplementationConstraint,
    decide_implementation,
)
from server.pm.repo_project import RepoOwner, RepoOwnership

OWNER = RepoOwner("id-a", "product-a")
COVERAGE = RepoOwnership("owner/repo", "owner/repo", "unique", OWNER, (OWNER,), (), "Unique")


def decision(**changes):
    args = dict(
        ownership=COVERAGE, requested_product_slug="product-a", policy={"dispatch": "auto"},
        trigger="automatic", effect="implementation", runner_mode="on", global_count=0,
        global_cap=2, product_count=0, daily_count=0, daily_cap=10, observed_at="2026-09-15T00:00:00Z",
    )
    args.update(changes)
    return decide_implementation(**args)


@pytest.mark.parametrize("mode,effect,trigger,expected,approval", [
    ("off", "implementation", "automatic", "refused", False),
    ("off", "proposal", "automatic", "refused", False),
    ("propose", "implementation", "automatic", "refused", True),
    ("propose", "proposal", "automatic", "permitted", False),
    ("auto", "implementation", "automatic", "permitted", False),
    ("auto", "proposal", "automatic", "permitted", False),
    ("off", "implementation", "manual", "permitted", False),
    ("propose", "implementation", "manual", "permitted", False),
])
def test_modes(mode, effect, trigger, expected, approval):
    result = decision(policy={"dispatch": mode}, effect=effect, trigger=trigger)
    assert result.state == expected
    assert result.approval_required is approval
    assert result.configured_dispatch == mode


@pytest.mark.parametrize("policy", [None, {}, {"weight": 2}, {"dispatch": "invalid"}])
def test_safe_defaults(policy):
    result = decision(policy=policy)
    assert result.state == "refused"
    assert result.configured_dispatch == "off"


@pytest.mark.parametrize("state,expected,code", [
    ("unresolved", "unknown", "ownership_unresolved"),
    ("ambiguous", "refused", "ownership_ambiguous"),
    ("missing", "refused", "ownership_missing"),
])
def test_no_implicit_fleet_fallback(state, expected, code):
    result = decision(ownership=replace(COVERAGE, state=state, owner=None))
    assert result.state == expected
    assert result.constraints[0].code == code
    assert result.product_slug is None


def test_unique_registration_without_verified_identity_is_not_authority():
    assert decision(ownership=replace(COVERAGE, canonical_repo=None)).state == "unknown"
    assert decision(ownership=None).state == "unknown"


def test_requested_product_must_match_owner_even_manual():
    result = decision(requested_product_slug="other", trigger="manual")
    assert result.state == "refused"
    assert result.constraints[0].code == "product_mismatch"


@pytest.mark.parametrize("key", ["global_count", "global_cap", "product_count", "daily_count", "daily_cap"])
@pytest.mark.parametrize("invalid", [None, -1, True])
def test_unknown_or_invalid_capacity_fails_closed(key, invalid):
    assert decision(**{key: invalid}).state == "unknown"


@pytest.mark.parametrize("changes", [
    {"global_count": 2}, {"product_count": 1}, {"daily_count": 10},
    {"global_cap": 0}, {"daily_cap": 0}, {"policy": {"dispatch": "auto", "max_in_flight": 0}},
])
def test_manual_does_not_bypass_caps(changes):
    assert decision(trigger="manual", runner_mode="off", **changes).state == "refused"


def test_existing_proposal_counts_once_and_a_new_claim_still_needs_headroom():
    assert decision(global_count=2, product_count=1, existing_slot=True).permitted
    assert not decision(global_count=2, product_count=1).permitted
    # `daily_budget` has no `occupied` exemption (not a per-slot ceiling; it
    # is not part of Ruling 20's "already holds its slot" guarantee), so it
    # still refuses regardless of `existing_slot`.
    assert decision(daily_count=10, existing_slot=True).state == "refused"


def test_ruling_20_an_occupied_claim_is_never_refused_on_capacity():
    """D8: admission refuses new claims; it never kills a session. A claim
    that already holds its slot (`existing_slot=True`) is permitted regardless
    of a moved-under-it ceiling — including a cap of 0 (paused) and a count
    that has since exceeded the cap. A NEW claim (`existing_slot=False`) is
    unaffected: a 0 cap still refuses it."""
    occupied_paused = decision(global_cap=0, existing_slot=True)
    assert occupied_paused.permitted
    assert any(c.code == "global_capacity_held" for c in occupied_paused.constraints)

    occupied_product_paused = decision(
        policy={"dispatch": "auto", "max_in_flight": 0}, existing_slot=True)
    assert occupied_product_paused.permitted
    assert any(c.code == "product_capacity_held" for c in occupied_product_paused.constraints)

    occupied_over_cap = decision(
        product_count=5, policy={"dispatch": "auto", "max_in_flight": 1}, existing_slot=True)
    assert occupied_over_cap.permitted
    assert any(c.code == "product_capacity_held" for c in occupied_over_cap.constraints)

    new_claim_paused = decision(global_cap=0, existing_slot=False)
    assert new_claim_paused.state == "refused"
    assert any(c.code == "global_capacity_full" for c in new_claim_paused.constraints)

    new_claim_product_paused = decision(
        policy={"dispatch": "auto", "max_in_flight": 0}, existing_slot=False)
    assert new_claim_product_paused.state == "refused"
    assert any(c.code == "product_capacity_full" for c in new_claim_product_paused.constraints)


def test_scheduled_runner_off_manual_and_unknown():
    assert decision(runner_mode="off").state == "refused"
    assert decision(runner_mode="off", trigger="manual").permitted
    assert decision(runner_mode=None, trigger="manual").state == "unknown"
    assert decision(runner_mode="bogus").state == "unknown"


def test_all_prerequisites_retained_in_stable_order_and_manual_cannot_bypass():
    gates = tuple(ImplementationConstraint(code, "refused", code, "/details") for code in (
        "target_owned", "human_hold", "engine_unsupported", "dependency_wait",
    ))
    result = decision(trigger="manual", prerequisites=gates)
    reordered = decision(trigger="manual", prerequisites=tuple(reversed(gates)))
    assert result == reordered
    assert result.state == "refused"
    assert [item.code for item in result.constraints[-4:]] == sorted(g.code for g in gates)
    assert all(item.link == "/details" for item in result.constraints[-4:])
    assert decision(prerequisites=(ImplementationConstraint("engine", "unknown", "Unknown"),)).state == "unknown"


def test_preview_scope_and_policy_revision():
    result = decision(remaining_checks=("engine", "target_ownership"))
    assert result.permitted
    assert result.remaining_checks == ("engine", "target_ownership")
    assert "not a future authorization token" in result.note
    assert result.product_slug == OWNER.slug
    assert result.product_id == OWNER.project_id
    assert result.policy_revision == decision().policy_revision
    assert result.policy_revision != decision(policy={"dispatch": "off"}).policy_revision
    assert result.observed_at == "2026-09-15T00:00:00Z"


def test_continued_work_is_not_admission():
    assert decision(starts_new_session=False).state == "unknown"
    assert decision(effect="proposal", starts_new_session=False).permitted
