"""Framework evidence gate: required evidence blocks SDLC artifact transitions.

The decision is pure over a default-branch policy snapshot; tolerance never
bypasses it, advisory findings never masquerade as failures, and exceptions
are bounded by subject, expiry and owner independence.
"""
import copy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from server.sdlc.engine import (
    Advance, ArtifactView, AwaitApproval, InitiativeView, MergeArtifact, NeedsHuman, next_action)
from server.sdlc.gates import (
    FRAMEWORK_EVIDENCE_UNREADABLE, framework_gate, framework_gate_for, observe_framework_evidence)
from server.tests.test_framework_receipts import configured, review

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
PR = 7


def gated(mode="evidence_required", *, tier="required_review", state="candidate",
          exceptions=None, concern_mode=None, **kw):
    """A loaded snapshot whose impeccable/ui-design concern runs in *mode*."""
    s = replace(configured(), **kw)
    entry = copy.deepcopy(s.frameworks[0])
    entry["state"] = state
    entry["observation"]["mode"] = mode
    entry["observation"]["concerns"]["ui-design"]["tier"] = tier
    if concern_mode is not None:
        entry["observation"]["concerns"]["ui-design"]["mode"] = concern_mode
    if exceptions is not None:
        entry["observation"]["exceptions"] = exceptions
    return replace(s, frameworks=(entry,))


def reviewed(s, **updates):
    return replace(s, reviews=(review(s, **updates),), reviews_complete=True)


def exception(**kw):
    value = {"concern": "ui-design", "pull_request": PR, "owner": "carol",
             "reason": "reviewer unavailable", "expires_at": "2026-10-09T00:00:00Z",
             "compensating_evidence": "manual walkthrough recorded on the PR"}
    value.update(kw)
    return value


def gate(s, *, pull_request=PR, now=NOW):
    return framework_gate(s, now, pull_request=pull_request)


# --------------------------------------------------------------------------- #
# Named deliverables                                                           #
# --------------------------------------------------------------------------- #

def test_required_framework_evidence_blocks_transition():
    decision = gate(gated())
    assert decision.blocking is True
    assert decision.reasons == ("impeccable/ui-design: required review missing",)
    # Fresh trusted evidence satisfies the same obligation.
    s = gated()
    assert gate(reviewed(s)).blocking is False
    # The engine turns the block into a human gate for both artifact transitions.
    ini = InitiativeView(slug="x", stage="design", status="active")
    open_pr = [ArtifactView(kind="spec", pr_number=PR)]
    merged = [ArtifactView(kind="spec", commit_sha="a" * 40, pr_number=PR)]
    reason = "; ".join(decision.reasons)
    for arts in (open_pr, merged):
        action = next_action(ini, arts, "on", "balanced", framework_block=reason)
        assert action == NeedsHuman(reason=f"framework evidence required: {reason}")


def test_advisory_finding_does_not_block():
    for mode in ("evidence_required", "enforced"):
        s = gated(mode, tier="advisory")
        assert gate(s).blocking is False
        assert gate(replace(s, changed_paths=(), scope_complete=False)).blocking is False
        row = observe_framework_evidence(s, now=NOW, pull_request=PR)["frameworks"][0]["obligations"][0]
        assert (row["tier"], row["blocking"]) == ("advisory", False)


@pytest.mark.parametrize("tolerance", ["balanced", "bold"])
def test_tolerance_cannot_skip_failed_evidence(tolerance):
    ini = InitiativeView(slug="x", stage="design", status="active")
    open_pr = [ArtifactView(kind="spec", pr_number=PR)]
    merged = [ArtifactView(kind="spec", commit_sha="a" * 40, pr_number=PR)]
    # Cautious already waits on the human tap; the block leaves that approval as is.
    for arts in (open_pr, merged):
        assert isinstance(next_action(ini, arts, "on", "cautious", framework_block="r"), AwaitApproval)
    for arts in (open_pr, merged):
        action = next_action(ini, arts, "on", tolerance, framework_block="impeccable/ui-design: x")
        assert isinstance(action, NeedsHuman)
        assert "framework evidence required" in action.reason
    # Without a block, bold still resolves both gates (the block is the only change).
    assert isinstance(next_action(ini, open_pr, "on", "bold"), MergeArtifact)
    assert isinstance(next_action(ini, merged, "on", "bold"), Advance)


def test_engine_block_preserves_other_decisions():
    """Only artifact merges/advances are gated: cautious approvals stay approvals
    when nothing is blocked, and build-stage advances are governed elsewhere."""
    ini = InitiativeView(slug="x", stage="design", status="active")
    assert isinstance(next_action(ini, [ArtifactView(kind="spec", pr_number=PR)], "on", "cautious"),
                      AwaitApproval)
    built = InitiativeView(slug="x", stage="build", status="active", issue="o/r#1",
                           epic="o/r#2", epic_total=1, epic_done=1)
    assert next_action(built, [], "on", "bold", framework_block="r") == Advance("deploy")
    blocked = InitiativeView(slug="x", stage="design", status="blocked")
    assert next_action(blocked, [], "on", "bold", framework_block="r") == NeedsHuman(
        reason="initiative is blocked")


# --------------------------------------------------------------------------- #
# Negative cases                                                               #
# --------------------------------------------------------------------------- #

def test_forged_producer_identity_never_satisfies():
    s = gated()
    claims = [{"framework": "impeccable", "repo": s.repo, "headSha": s.head_sha,
               "policyRevision": s.policy_revision, "producer": "trusted-runner", "reviewer": "bob"}]
    assert observe_framework_evidence(s, claims, now=NOW, pull_request=PR)["blocking"] is True
    forged = review(s)
    forged["author"]["login"] = "mallory"
    assert gate(replace(s, reviews=(forged,), reviews_complete=True)).blocking is True


def test_author_self_review_never_satisfies():
    s = gated()
    own = review(s)
    own["author"]["login"] = "alice"   # the PR author in the fixture
    entry = copy.deepcopy(s.frameworks[0])
    entry["observation"]["trusted_github_reviewers"] = ["bob", "alice"]
    assert gate(replace(s, frameworks=(entry,), reviews=(own,), reviews_complete=True)).blocking is True


def test_stale_head_review_never_satisfies():
    s = gated()
    stale = review(s)
    stale["commit"]["oid"] = "f" * 40
    assert gate(replace(s, reviews=(stale,), reviews_complete=True)).blocking is True


def test_wrong_repository_record_never_satisfies():
    assert gate(reviewed(gated(), repo="other/repo")).blocking is True


def test_policy_downgrade_in_the_same_pr_is_ignored():
    """The gate reads only the snapshot's default-branch declarations: a PR that
    rewrites its own lock to observe changes the scope, not the obligation."""
    base = gated(changed_paths=("web/app.tsx",))
    downgrade = gated(changed_paths=("web/app.tsx", ".agents/frameworks.lock.json"))
    assert gate(downgrade) == gate(base)
    assert gate(downgrade).blocking is True
    # A lock-only PR is unknown scope for this rule: enforced still blocks.
    lock_only = gated("enforced", changed_paths=(".agents/frameworks.lock.json",))
    assert gate(lock_only).reasons == ("impeccable/ui-design: applicability unknown (needs triage)",)


def test_stale_review_is_waivable_after_the_head_moves():
    s = gated(exceptions=[exception()])
    moved = replace(s, reviews=(review(s),), reviews_complete=True, head_sha="9" * 40)
    from server.sdlc.framework_evidence import review_observation
    assert review_observation(moved, moved.frameworks[0], "ui-design") == "stale"
    assert gate(moved).blocking is False
    unexcepted = replace(moved, frameworks=gated().frameworks)
    assert gate(unexcepted).reasons == ("impeccable/ui-design: required review stale",)


def test_lock_edit_makes_another_prs_review_stale_not_invalid():
    from server.sdlc.framework_evidence import review_observation
    s = gated()
    edited = replace(s, reviews=(review(s),), reviews_complete=True, framework_digest="1" * 64)
    assert review_observation(edited, edited.frameworks[0], "ui-design") == "stale"


def test_malformed_review_stays_invalid():
    from server.sdlc.framework_evidence import review_observation
    s = gated()
    for updates in ({"repo": "x/y"}, {"findings": None}, {"version": 2}):
        bad = replace(s, reviews=(review(s, **updates),), reviews_complete=True)
        assert review_observation(bad, bad.frameworks[0], "ui-design") == "invalid", updates


def test_bot_suffix_is_normalized_for_author_comparisons():
    from server.sdlc.framework_evidence import review_observation
    bot = gated(exceptions=[exception(owner="renovate[bot]")], author="renovate")
    assert gate(bot).blocking is True          # self-issued once [bot] is stripped
    s = gated(author="carol[bot]")
    entry = copy.deepcopy(s.frameworks[0])
    entry["observation"]["trusted_github_reviewers"] = ["carol"]
    own = review(s)
    own["author"]["login"] = "carol"
    own_review = replace(s, frameworks=(entry,), reviews=(own,), reviews_complete=True)
    assert review_observation(own_review, entry, "ui-design") == "missing"


def test_expired_exception_does_nothing():
    s = gated(exceptions=[exception(expires_at="2026-10-08T12:00:00Z")])
    assert gate(s).blocking is True
    assert gate(gated(exceptions=[exception()])).blocking is False


def test_self_issued_exception_does_nothing():
    assert gate(gated(exceptions=[exception(owner="Alice")])).blocking is True
    assert gate(replace(gated(exceptions=[exception()]), author=None)).blocking is True


def test_exception_for_another_pr_does_nothing():
    s = gated(exceptions=[exception(pull_request=8)])
    assert gate(s).blocking is True
    assert gate(gated(exceptions=[exception()]), pull_request=None).blocking is True


def test_exception_cannot_waive_invalid_review():
    s = gated(exceptions=[exception()])
    bad = review(s)
    bad["state"] = "CHANGES_REQUESTED"      # a trusted reviewer objects
    decision = gate(replace(s, reviews=(bad,), reviews_complete=True))
    assert decision.reasons == ("impeccable/ui-design: required review invalid",)


@pytest.mark.parametrize("withdraw", [{"state": "DISMISSED"}, {"isMinimized": True}])
def test_withdrawn_review_is_absent_evidence_and_waivable(withdraw):
    from server.sdlc.framework_evidence import review_observation
    s = gated()
    record = review(s)
    record.update(withdraw)
    withdrawn = replace(s, reviews=(record,), reviews_complete=True)
    assert review_observation(withdrawn, withdrawn.frameworks[0], "ui-design") == "missing"
    assert gate(withdrawn).reasons == ("impeccable/ui-design: required review missing",)
    excepted = replace(withdrawn, frameworks=gated(exceptions=[exception()]).frameworks)
    assert gate(excepted).blocking is False


@pytest.mark.parametrize("state", ["revoked", "rejected"])
def test_revoked_framework_blocks_in_scope_until_excepted(state):
    # Applicable: blocks even with a recorded review; a valid exception waives it.
    s = reviewed(gated(state=state))
    assert gate(s).reasons == ("impeccable/ui-design: framework revoked",)
    assert gate(reviewed(gated(state=state, exceptions=[exception()]))).blocking is False
    # Unknown scope under enforced already blocks; revocation names the reason.
    unknown = ("server/db/a.py",)
    assert gate(gated("enforced", state=state, changed_paths=unknown)).reasons == (
        "impeccable/ui-design: framework revoked",)


@pytest.mark.parametrize("state", ["revoked", "rejected"])
def test_revoked_framework_does_not_block_out_of_scope(state):
    for mode in ("evidence_required", "enforced"):
        assert gate(gated(mode, state=state, changed_paths=("docs/a.md",))).blocking is False
    assert gate(gated("evidence_required", state=state,
                      changed_paths=("server/db/a.py",))).blocking is False
    assert gate(gated("observe", state=state)).blocking is False


def test_unknown_applicability_blocks_only_when_enforced():
    unknown = dict(changed_paths=("server/db/a.py",))
    assert gate(gated("evidence_required", **unknown)).blocking is False
    assert gate(gated("enforced", **unknown)).blocking is True
    assert gate(gated("enforced", exceptions=[exception()], **unknown)).blocking is False
    incomplete = dict(changed_paths=(), scope_complete=False)
    assert gate(gated("evidence_required", **incomplete)).blocking is False
    assert gate(gated("enforced", **incomplete)).blocking is True


def test_not_applicable_never_blocks():
    assert gate(gated("enforced", changed_paths=("docs/a.md",))).blocking is False


def test_deterministic_tier_is_never_satisfied():
    s = gated(tier="deterministic")
    assert gate(reviewed(s)).reasons == (
        "impeccable/ui-design: deterministic evidence unavailable (no trusted runner ingestion)",)


def test_concern_mode_overrides_entry_default():
    assert gate(gated("observe", concern_mode="enforced")).blocking is True
    assert gate(gated("enforced", concern_mode="observe")).blocking is False


def test_observe_mode_never_blocks():
    for state in ("candidate", "revoked"):
        for paths in (("web/app.tsx",), ("server/db/a.py",), ()):
            assert gate(gated("observe", state=state, changed_paths=paths)).blocking is False


@pytest.mark.parametrize("status", ["no_contract", "legacy_contract", "unavailable"])
def test_unloaded_policy_is_not_blocking(status):
    assert gate(replace(gated("enforced"), status=status)).blocking is False


def test_observation_payload_reports_modes_and_blocking():
    observed = observe_framework_evidence(gated(), now=NOW, pull_request=PR)
    assert observed["mode"] == "evidence_required"
    assert observed["blocking"] is True
    assert observed["gateReasons"] == ["impeccable/ui-design: required review missing"]
    assert not observed["reason"].startswith("Observe only")
    row = observed["frameworks"][0]["obligations"][0]
    assert (row["mode"], row["blocking"]) == ("evidence_required", True)
    s = configured()
    process = {"id": "process", "state": "candidate", "concerns": ["process"],
               "observation": {"mode": "enforced", "trusted_github_reviewers": [],
                               "concerns": {"process": {"tier": "advisory", "include_paths": [],
                                                        "exclude_paths": []}}}}
    mixed = observe_framework_evidence(replace(s, frameworks=(*s.frameworks, process)))
    assert (mixed["mode"], mixed["blocking"]) == ("mixed", False)
    plain = observe_framework_evidence(s)
    assert (plain["mode"], plain["blocking"], plain["gateReasons"]) == ("observe", False, [])
    assert plain["reason"].startswith("Observe only")


# --------------------------------------------------------------------------- #
# Default synthesizer seam                                                     #
# --------------------------------------------------------------------------- #

def _patch_reads(monkeypatch, policy_only, with_pr):
    from server.sdlc import framework_observe
    calls = []

    def fake(repo, *, pull_request=None, run=None, token=None):
        calls.append(pull_request)
        result = with_pr if pull_request else policy_only
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(framework_observe, "load_policy_snapshot", fake)
    return framework_observe.read_framework_gate, calls


@pytest.mark.asyncio
async def test_seam_skips_the_pr_read_without_a_gating_concern(monkeypatch):
    for policy_only in (gated("observe"), replace(gated(), status="no_contract"),
                        replace(gated(), status="legacy_contract"), gated(tier="advisory")):
        read, calls = _patch_reads(monkeypatch, policy_only, AssertionError("not read"))
        assert await read("o/r", PR) is None
        assert calls == [None]


@pytest.mark.asyncio
async def test_seam_returns_joined_reasons_or_none(monkeypatch):
    s = gated()
    read, calls = _patch_reads(monkeypatch, s, s)
    assert await read("o/r", PR) == "impeccable/ui-design: required review missing"
    assert calls == [None, PR]
    read, _ = _patch_reads(monkeypatch, s, reviewed(s))
    assert await read("o/r", PR) is None


@pytest.mark.asyncio
async def test_seam_fails_closed_once_an_obligation_is_known(monkeypatch):
    s = gated()
    for with_pr in (RuntimeError("boom"), replace(s, status="unavailable"),
                    replace(s, changed_paths=(), scope_complete=False),
                    replace(s, head_sha=None)):
        read, _ = _patch_reads(monkeypatch, s, with_pr)
        assert await read("o/r", PR) == "framework evidence unreadable"


@pytest.mark.asyncio
async def test_seam_blocks_when_the_policy_read_fails(monkeypatch):
    read, calls = _patch_reads(monkeypatch, replace(gated(), status="unavailable", frameworks=()),
                               AssertionError("not read"))
    assert await read("o/r", PR) == FRAMEWORK_EVIDENCE_UNREADABLE
    assert calls == [None]


def test_api_observation_and_loop_share_the_unreadable_rule():
    """The detail observation with a PR applies the same rule as the loop's seam."""
    s = gated()
    for pr_snapshot in (replace(s, status="unavailable"), replace(s, head_sha=None),
                        replace(s, changed_paths=(), scope_complete=False)):
        assert framework_gate_for(s, pr_snapshot, NOW, pull_request=PR).reasons == (
            FRAMEWORK_EVIDENCE_UNREADABLE,)
        observed = observe_framework_evidence(pr_snapshot, now=NOW, pull_request=PR)
        assert observed["blocking"] is True
        assert FRAMEWORK_EVIDENCE_UNREADABLE in observed["gateReasons"]
    assert framework_gate_for(replace(s, status="unavailable"), None, NOW,
                              pull_request=PR).reasons == (FRAMEWORK_EVIDENCE_UNREADABLE,)
    assert framework_gate_for(gated("observe"), None, NOW, pull_request=PR).blocking is False
