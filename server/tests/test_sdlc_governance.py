"""Pure SDLC governance core (Phase 3, issue #805).

Exhaustive, I/O-free — the way test_sdlc_engine tests next_action. Covers the
permission-tier ladder, the prod approval gate (incl. separation of duties), and
the managed non-negotiables drift guard.
"""
from __future__ import annotations

import pytest

from server.sdlc.governance import (
    ENVIRONMENTS,
    MANAGED_DENY,
    ReleaseDecision,
    describe_tiers,
    evaluate_release,
    managed_deny_patterns,
    missing_managed_rules,
    permission_tier,
)
from server.sessions.types import VALID_SESSION_MODES


# --------------------------------------------------------------------------- #
# Permission tiers by environment                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("env,mode", [
    ("dev", "full-auto"),
    ("staging", "acceptEdits"),
    ("prod", "interactive"),
])
def test_permission_tier_ladder(env, mode):
    assert permission_tier(env) == mode


def test_every_tier_is_a_valid_session_mode():
    # The ladder can never drift into a mode the permission engine can't honour.
    for env in ENVIRONMENTS:
        assert permission_tier(env) in VALID_SESSION_MODES


def test_prod_is_the_most_restrictive_tier():
    # dev auto-approves shell; prod asks for everything.
    assert permission_tier("dev") == "full-auto"
    assert permission_tier("prod") == "interactive"


def test_unknown_environment_raises():
    with pytest.raises(ValueError):
        permission_tier("production")  # typo — must not silently pass


def test_describe_tiers_is_a_copy():
    d = describe_tiers()
    d["dev"] = "tampered"
    assert permission_tier("dev") == "full-auto"  # internal map untouched


# --------------------------------------------------------------------------- #
# Approval gate                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("env", ["dev", "staging"])
def test_non_prod_is_never_gated(env):
    d = evaluate_release(environment=env, release_approval=None)
    assert d.allowed is True
    assert d.route == ""


def test_prod_without_approval_is_blocked():
    d = evaluate_release(environment="prod", release_approval=None)
    assert isinstance(d, ReleaseDecision)
    assert d.allowed is False
    assert "RELEASE_APPROVAL" in d.reason
    assert d.route  # a concrete unblock route is surfaced


def test_prod_with_empty_approval_is_blocked():
    d = evaluate_release(environment="prod", release_approval="   ")
    assert d.allowed is False


def test_prod_with_approval_is_allowed():
    d = evaluate_release(environment="prod", release_approval="incident-42")
    assert d.allowed is True
    assert d.route == ""


def test_prod_self_approval_blocked_separation_of_duties():
    # The agent that authored the change cannot approve its own release.
    d = evaluate_release(
        environment="prod",
        release_approval="incident-42",
        author="backlog_runner",
        approver="backlog_runner",
    )
    assert d.allowed is False
    assert "separation of duties" in d.reason.lower()
    assert d.route


def test_prod_distinct_approver_allowed():
    d = evaluate_release(
        environment="prod",
        release_approval="incident-42",
        author="backlog_runner",
        approver="tony",
    )
    assert d.allowed is True


def test_prod_identity_check_skipped_when_unknown():
    # No identity context (manual human deploy): a bare RELEASE_APPROVAL passes.
    d = evaluate_release(environment="prod", release_approval="ok", author=None, approver=None)
    assert d.allowed is True


# --------------------------------------------------------------------------- #
# Managed non-negotiables                                                       #
# --------------------------------------------------------------------------- #

def test_managed_deny_includes_force_push_and_constitution():
    patterns = managed_deny_patterns()
    assert any("force" in p for p in patterns)
    assert any("constitution.md" in p for p in patterns)
    assert any("managed-settings.json" in p for p in patterns)


def test_missing_managed_rules_reports_all_when_empty_settings():
    missing = missing_managed_rules({})
    assert missing == managed_deny_patterns()


def test_missing_managed_rules_empty_when_all_present():
    settings = {"permissions": {"deny": managed_deny_patterns()}}
    assert missing_managed_rules(settings) == []


def test_missing_managed_rules_reports_the_gap():
    partial = managed_deny_patterns()[1:]  # drop the first non-negotiable
    settings = {"permissions": {"deny": partial}}
    assert missing_managed_rules(settings) == [managed_deny_patterns()[0]]


def test_managed_rules_carry_principle_and_rationale():
    for rule in MANAGED_DENY:
        assert rule.pattern and rule.principle and rule.why
