"""server/pm/triage_accept: the pure auto-accept gate (spec §8, §13)."""
from __future__ import annotations

import pytest

from server.pm.classification import CONFIDENCE_LEVELS
from server.pm.triage_accept import auto_accept_eligible


def _ok(**over):
    base = dict(tier="high", confidence_level="high", proposed_feature_id="f1",
                feature_state="live", auto_undone=None)
    base.update(over)
    return auto_accept_eligible(**base)


def test_confidence_levels_are_the_one_ascending_vocabulary():
    assert CONFIDENCE_LEVELS == ("low", "medium", "high")


def test_high_tier_accepts_only_high():
    assert _ok(tier="high", confidence_level="high")
    assert not _ok(tier="high", confidence_level="medium")
    assert not _ok(tier="high", confidence_level="low")


def test_medium_tier_includes_high():
    assert _ok(tier="medium", confidence_level="medium")
    assert _ok(tier="medium", confidence_level="high")
    assert not _ok(tier="medium", confidence_level="low")


def test_off_never_accepts():
    assert not _ok(tier="off", confidence_level="high")


@pytest.mark.parametrize("level", [None, "", "HIGH", "certain", "very high"])
def test_a_confidence_outside_the_vocabulary_reads_as_low(level):
    # Spec §13: vocabulary drift is treated as low, never accepted.
    assert not _ok(tier="medium", confidence_level=level)


def test_never_a_no_fit_proposal():
    assert not _ok(proposed_feature_id=None)


def test_never_a_proposal_whose_capability_is_still_proposed():
    assert not _ok(feature_state="proposed")
    assert _ok(feature_state="discovery")
    assert _ok(feature_state="delivering")


def test_a_missing_feature_state_is_not_eligible():
    # The target row is gone (or unknown): a human decides, not the machine.
    assert not _ok(feature_state=None)


def test_an_undone_auto_accept_is_never_retried():
    assert not _ok(auto_undone=True)
    assert _ok(auto_undone=False)
    assert _ok(auto_undone=None)


# --------------------------------------------------------------------------- #
# The shared pending-proposal SELECT (row lock)                                #
# --------------------------------------------------------------------------- #


def test_pending_proposal_stmt_takes_a_row_lock_on_postgres():
    """The human accept route and the loop's auto-accept seam must read the
    proposal under a row lock, or both can pass the ``status == "pending"``
    check and last-writer-wins (a human's edits silently overwritten by the
    loop's un-edited copy, ``classified_by`` reading "auto" for a human's
    decision).

    Compiled for the postgresql dialect because that is where the lock exists:
    SQLAlchemy's SQLite dialect renders ``with_for_update()`` as nothing at all,
    so a SQLite-only assertion could not tell a locked statement from an
    unlocked one — and every test here runs on SQLite."""
    from sqlalchemy.dialects import postgresql

    from server.pm.triage_accept import pending_proposal_stmt

    sql = str(pending_proposal_stmt("p1").compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_pending_proposal_stmt_is_the_one_builder_both_accept_paths_use():
    """Constitution VI: one statement builder, not a copy per call site. Both
    the router's ``_load_pending_proposal`` and the orchestrator's auto-accept
    seam import THIS function — a second hand-rolled ``select(TriageProposal)``
    at either site would silently drop the lock the test above guards."""
    import inspect

    from server.api import pm as pm_api
    from server.pm import triage_accept
    from server.sessions import orchestrator

    assert pm_api.pending_proposal_stmt is triage_accept.pending_proposal_stmt
    seam = inspect.getsource(orchestrator.SessionOrchestrator._pm_triage_auto_accept)
    assert "pending_proposal_stmt(proposal_id)" in seam
    assert "select(TriageProposal)" not in seam
    router = inspect.getsource(pm_api._load_pending_proposal)
    assert "pending_proposal_stmt(proposal_id)" in router
    assert "select(TriageProposal)" not in router


def test_restore_overlay_ignores_the_provenance_key():
    """``_restore_overlay`` reads named keys only, so the extra ``decidedBy``
    rides along harmlessly: the overlay comes back exactly as it was and nothing
    sets a ``decidedBy`` attribute on the WorkItem."""
    from server.db.models import WorkItem
    from server.pm.triage_accept import _overlay_snapshot, _restore_overlay

    before = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="open",
                      feature_id="f-old", reach=3, impact=2, effort=1, confidence=80,
                      triage_state="proposed", classified_by="human", classified_at=None)
    snap = _overlay_snapshot(before)
    assert "decidedBy" not in snap          # the snapshot helper's own contract
    snap["decidedBy"] = "auto"              # what apply_accept adds beside it

    after = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="open",
                     feature_id="f-new", reach=9, impact=9, effort=9, confidence=9,
                     triage_state="classified", classified_by="auto")
    _restore_overlay(after, snap)
    assert (after.feature_id, after.reach, after.impact, after.effort,
            after.confidence) == ("f-old", 3, 2, 1, 80)
    assert after.triage_state == "proposed" and after.classified_by == "human"
    assert after.classified_at is None
    assert not hasattr(after, "decidedBy")


def test_restore_overlay_converts_a_pre_ladder_needs_human_snapshot():
    """An undo of a proposal accepted BEFORE the ladder landed replays a snapshot whose
    ``triageState`` is the removed ``needs_human``. Restoring it verbatim would write a
    state no longer in the vocabulary, so the branch converts it to what it meant: an
    ``unclassified`` item under an ``owner_action`` hold stamped by ``migration``, the
    same mapping ``LEGACY_CONVERSION_SQL`` applies to live rows (final-review M12)."""
    from server.db.models import WORK_ITEM_TRIAGE_STATES, WorkItem
    from server.pm.triage_accept import _restore_overlay

    wi = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="open",
                  triage_state="classified", classified_by="auto",
                  hold_kind="evidence", hold_reason="repro", hold_by="session:s1")
    _restore_overlay(wi, {"triageState": "needs_human", "featureId": None,
                          "classifiedBy": "human", "classifiedAt": None})
    assert wi.triage_state == "unclassified"
    assert wi.triage_state in WORK_ITEM_TRIAGE_STATES
    assert wi.hold_kind == "owner_action"
    assert wi.hold_by == "migration"
    assert wi.hold_reason == "restored pre-ladder needs_human"
    assert wi.hold_set_at is None and wi.hold_fingerprint is None


def test_dispatch_off_accept_waives_only_the_confidence_bar():
    assert _ok(tier="off", confidence_level="low", dispatch_off_accept=True)
    assert not _ok(tier="off", confidence_level="low", dispatch_off_accept=True,
                   proposed_feature_id=None)
    assert not _ok(tier="off", dispatch_off_accept=True, feature_state="proposed")
    assert not _ok(tier="off", dispatch_off_accept=True, auto_undone=True)


def test_bot_artifact_titles():
    from server.pm.triage_accept import is_bot_artifact
    assert is_bot_artifact("UI Maintenance Cost & Architecture ROI Digest")
    assert is_bot_artifact("docs(fix-patterns): regenerate index")
    assert not is_bot_artifact("docs(fix-patterns): add a pattern")
    assert not is_bot_artifact(None)


# --------------------------------------------------------------------------- #
# #2683 (follow-up): the repeat rule, the rule name, bot authors, the dismiss   #
# write path                                                                   #
# --------------------------------------------------------------------------- #


def test_eligible_names_the_rule_that_admitted_it():
    from server.pm.triage_accept import RULE_CONFIDENCE, RULE_DISPATCH_OFF, RULE_REPEAT
    assert _ok(tier="high", confidence_level="high") == RULE_CONFIDENCE
    assert _ok(tier="off", confidence_level="low", dispatch_off_accept=True) == RULE_DISPATCH_OFF
    assert _ok(tier="off", confidence_level="low", repeat_threshold=3,
               prior_human_accepts=3) == RULE_REPEAT
    assert _ok(tier="off", confidence_level="low") is None


def test_repeat_rule_waives_the_confidence_bar_and_the_proposed_guard():
    """89 human accepts onto a still-``proposed`` capability ARE the endorsement."""
    from server.pm.triage_accept import RULE_REPEAT
    assert _ok(tier="off", confidence_level="low", feature_state="proposed",
               repeat_threshold=3, prior_human_accepts=89) == RULE_REPEAT
    assert _ok(tier="off", confidence_level=None, repeat_threshold=3,
               prior_human_accepts=3) == RULE_REPEAT


def test_repeat_rule_needs_the_threshold_and_is_off_at_zero():
    assert _ok(tier="off", confidence_level="low", repeat_threshold=3,
               prior_human_accepts=2) is None
    assert _ok(tier="off", confidence_level="low", repeat_threshold=0,
               prior_human_accepts=500) is None


def test_repeat_rule_keeps_the_structural_guards():
    base = dict(tier="off", confidence_level="low", repeat_threshold=1, prior_human_accepts=5)
    assert _ok(**base, proposed_feature_id=None) is None        # never a no-fit
    assert _ok(**base, auto_undone=True) is None                # never re-decide an undo
    assert _ok(**base, feature_state=None) is None              # no feature row
    assert _ok(**base, feature_state="sunset") is None          # a retired capability


def test_repeat_threshold_applies_only_to_a_product_that_opted_in_to_auto_accept():
    from server.automations.product_policy import effective_policy
    from server.pm.triage_accept import repeat_threshold_for
    base = effective_policy(None)
    assert base["triage_auto_accept_repeat"] == 3
    assert repeat_threshold_for(base) == 0                       # never opted in
    assert repeat_threshold_for({**base, "triage_auto_accept": "high"}) == 3
    assert repeat_threshold_for({**base, "triage_auto_accept_dispatch_off": True}) == 3
    assert repeat_threshold_for({**base, "triage_auto_accept": "medium",
                                 "triage_auto_accept_repeat": 0}) == 0
    assert repeat_threshold_for(None) == 0


def test_dispatch_off_opt_in_does_not_arm_the_repeat_rule_while_dispatch_is_on():
    """The Dispatch-off flag consents to accepts "while nothing is dispatched".
    With Dispatch ON a repeat accept becomes Ready and gets dispatched — that is
    not what the product agreed to, so the flag alone is no opt-in then."""
    from server.automations.product_policy import effective_policy
    from server.pm.triage_accept import repeat_threshold_for
    on = {**effective_policy(None), "dispatch": "auto", "triage_auto_accept": "off",
          "triage_auto_accept_dispatch_off": True}
    assert repeat_threshold_for(on) == 0
    assert repeat_threshold_for({**on, "triage_auto_accept": "high"}) == 3


@pytest.mark.parametrize("author", ["dependabot[bot]", "renovate[bot]"])
def test_bot_authors_are_bot_artifacts_whatever_the_title(author):
    from server.pm.triage_accept import is_bot_artifact
    assert is_bot_artifact("Fix the login bug", author)


@pytest.mark.parametrize("author", [None, "", "tony", "app/viberpit", "github-actions",
                                    "app/github-actions"])
def test_people_our_own_app_and_the_ci_alert_filer_are_not_bot_authors(author):
    """``app/viberpit`` files the auto-error stream and ``github-actions`` the
    recurring CI alert issues (#2680) — both real work the runner takes on."""
    from server.pm.triage_accept import is_bot_artifact
    assert not is_bot_artifact("Weekly failed — 2026-10-10", author)


@pytest.mark.parametrize("author", [None, "", "tony", "dependabot[bot]", "github-actions",
                                    "app/github-actions", "app/dependabot"])
def test_the_author_leg_is_the_queues_own_bot_rule(author):
    """Constitution VI: triage and the queue judge bot authorship by ONE rule."""
    from server.pm.queue import is_bot_noise
    from server.pm.triage_accept import is_bot_artifact
    title = "an ordinary title"
    assert is_bot_artifact(title, author) == is_bot_noise(author=author or "", labels=(),
                                                          title=title)


def test_extra_patterns_match_case_insensitively_and_a_bad_one_is_ignored():
    from server.pm.triage_accept import is_bot_artifact
    pats = [r"^chore: audit codebase for\b", "(unclosed"]
    assert is_bot_artifact("CHORE: Audit codebase for retry-storm failure mode", "tony", pats)
    assert not is_bot_artifact("chore: tidy imports", "tony", pats)
    assert is_bot_artifact("docs(fix-patterns): regenerate index", "tony", pats)


def test_dismissed_is_a_proposal_status():
    from server.db.models import DECISION_PROPOSAL_STATUSES, TRIAGE_PROPOSAL_STATUSES
    assert "dismissed" in TRIAGE_PROPOSAL_STATUSES
    # A decision proposal is accepted or rejected, never dismissed.
    assert "dismissed" not in DECISION_PROPOSAL_STATUSES


def test_closed_items_are_not_awaiting_a_decision():
    from server.pm.triage_accept import item_is_open
    assert item_is_open("open") and item_is_open(None)
    assert not item_is_open("closed") and not item_is_open("merged")


@pytest.mark.asyncio
async def test_apply_dismiss_resolves_without_a_hold_or_a_feature(tmp_path, monkeypatch):
    from sqlalchemy import select

    from server.db import db_session, init_db
    from server.db.models import AuditEvent, TriageProposal, WorkItem
    from server.pm.triage_accept import (DECIDED_BY_HUMAN, DISMISSED_TRIAGE_STATE,
                                         apply_dismiss)
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("VIBERPIT_DB_URL", url)
    await init_db(url)
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="open",
                      triage_state="proposed", author="tony")
        db.add(wi)
        await db.flush()
        prop = TriageProposal(work_item_id=wi.id, status="pending", proposed_feature_id=None,
                              confidence_level="high")
        db.add(prop)
        await db.flush()
        await apply_dismiss(db, prop, decided_by=DECIDED_BY_HUMAN, audit_identity="tester",
                            reason="noise")
        pid, wid = prop.id, wi.id
    async with db_session() as db:
        prop = await db.get(TriageProposal, pid)
        wi = await db.get(WorkItem, wid)
        assert prop.status == "dismissed" and prop.resolved_at is not None
        assert prop.undo_snapshot["decidedBy"] == DECIDED_BY_HUMAN
        assert wi.hold_kind is None and wi.feature_id is None
        assert wi.triage_state == DISMISSED_TRIAGE_STATE
        audits = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.triage.dismiss"))).scalars().all()
        assert [a.detail for a in audits] == [{"proposalId": pid, "workItemId": wid,
                                               "reason": "noise", "decidedBy": "human"}]


@pytest.mark.asyncio
async def test_a_requeued_dismissal_leaves_the_item_classifiable(tmp_path, monkeypatch):
    """A closed-item or bot-artifact dismissal must not strand the item: it goes
    back to ``unclassified`` so a reopened item (or a product that turns bot
    dismissal off) is triaged again. The proposal still resolves ``dismissed``."""
    from server.db import db_session, init_db
    from server.db.models import TriageProposal, WorkItem
    from server.pm.triage_accept import DECIDED_BY_AUTO, apply_dismiss
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("VIBERPIT_DB_URL", url)
    await init_db(url)
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="closed",
                      triage_state="proposed", author="tony")
        db.add(wi)
        await db.flush()
        prop = TriageProposal(work_item_id=wi.id, status="pending")
        db.add(prop)
        await db.flush()
        await apply_dismiss(db, prop, decided_by=DECIDED_BY_AUTO, audit_identity="pm-triage",
                            reason="item_closed", requeue=True)
        pid, wid = prop.id, wi.id
    async with db_session() as db:
        assert (await db.get(TriageProposal, pid)).status == "dismissed"
        wi = await db.get(WorkItem, wid)
        assert wi.triage_state == "unclassified" and wi.hold_kind is None
        assert (await db.get(TriageProposal, pid)).undo_snapshot["triageState"] == "proposed"


def test_sets_aside_bot_output_is_the_one_policy_gate():
    """Recognizer + the product's switch + its own patterns, in ONE call — the
    loop, the onboarding classify self-select and the onboarding count share it."""
    from server.automations.product_policy import effective_policy
    from server.pm.triage_accept import sets_aside_bot_output
    on = effective_policy(None)                                   # dismissal defaults on
    off = {**on, "triage_dismiss_bot_artifacts": False}
    mine = {**on, "triage_bot_artifact_patterns": [r"^chore: audit codebase for\b"]}
    assert sets_aside_bot_output(on, "docs(fix-patterns): regenerate index", "tony")
    assert sets_aside_bot_output(on, "Fix it", "dependabot[bot]")
    assert not sets_aside_bot_output(on, "Weekly failed — 2026-10-10", "app/github-actions")
    assert not sets_aside_bot_output(off, "docs(fix-patterns): regenerate index", "tony")
    assert not sets_aside_bot_output(None, "docs(fix-patterns): regenerate index", "tony")
    assert sets_aside_bot_output(mine, "chore: audit codebase for X", "tony")
    assert not sets_aside_bot_output(on, "chore: audit codebase for X", "tony")
