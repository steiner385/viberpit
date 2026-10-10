"""Pure ranking core (spec 003 §7a). No I/O — rows in, ordered queue + skip counts out."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.pm.desired_state import STATUS_OPTIONS, STATUS_READY
from server.pm.queue import (MIN_STATUS_OPTIONS, SKIP_REASONS, URGENT_LABELS, QueueRow,
                             is_trusted_author, rank_key, to_candidate)
from server.pm.queue import rank as _rank
from server.pm.queue import skip_reason as _skip_reason

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


# The trust gate is fail-closed, so every fixture row (default author "tony") must be
# vouched for; these wrappers declare it once. A test of the gate itself passes
# ``trusted_authors=`` explicitly, which wins.
def skip_reason(row, **kw):
    kw.setdefault("trusted_authors", frozenset({"tony"}))
    return _skip_reason(row, **kw)


def rank(rows, **kw):
    kw.setdefault("trusted_authors", frozenset({"tony"}))
    return _rank(rows, **kw)


def _row(**over):
    base = dict(ref="o/r#1", repo="o/r", number=1, product="viberpit", title="t", body="", url="u",
                status="Ready", score=10.0, gh_created_at=NOW - timedelta(days=1), gh_updated_at=NOW,
                assignees=(), author="tony", labels=(), open_children=0, open_blockers=(),
                live_session=False, claimed=False, hold_kind=None)
    base.update(over)
    return QueueRow(**base)


def test_ready_unblocked_unassigned_row_is_eligible():
    assert skip_reason(_row(), min_status="Ready", respect_dependencies=True, now=NOW) is None


def test_status_gate_honours_min_status():
    kw = dict(respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status="Backlog"), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Backlog"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Blocked"), min_status="Backlog", **kw) == "blocked"
    assert skip_reason(_row(status="Done"), min_status="Backlog", **kw) == "status"


def test_open_blocker_blocks_unless_dependencies_are_ignored():
    kw = dict(min_status="Ready", now=NOW)
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=True, **kw) == "blocked"
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=False, **kw) is None


def test_live_session_claim_assignee_epic_and_bot_are_excluded():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(live_session=True), **kw) == "live_session"
    assert skip_reason(_row(claimed=True), **kw) == "claimed"
    assert skip_reason(_row(assignees=("steiner385",)), **kw) == "assigned"
    assert skip_reason(_row(open_children=3), **kw) == "epic_open"
    assert skip_reason(_row(author="renovate[bot]"), **kw) == "bot"
    assert skip_reason(_row(labels=("dependencies",)), **kw) == "bot"
    assert skip_reason(_row(title="Dependency Dashboard"), **kw) == "bot"


def test_extra_skip_labels_add_to_the_global_bot_filter_without_replacing_it():
    """Per-product ``skip_labels`` (product_policy schema) is additive: it never
    turns off the built-in dependencies/renovate filter, it only adds to it."""
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW)
    # A product-specific label skips only when passed.
    assert skip_reason(_row(labels=("on-hold",)), **kw) is None
    assert skip_reason(_row(labels=("on-hold",)), extra_skip_labels=frozenset({"on-hold"}),
                       **kw) == "bot"
    # Case-insensitive, matching the global filter's own comparison.
    assert skip_reason(_row(labels=("On-Hold",)), extra_skip_labels=frozenset({"on-hold"}),
                       **kw) == "bot"
    # The global filter still fires with no product labels configured at all.
    assert skip_reason(_row(labels=("dependencies",)), extra_skip_labels=frozenset(),
                       **kw) == "bot"
    # And still fires even when the product has its OWN unrelated list configured.
    assert skip_reason(_row(labels=("renovate",)), extra_skip_labels=frozenset({"on-hold"}),
                       **kw) == "bot"


def test_held_rows_are_skipped_regardless_of_time():
    """Spec 2026-09-18 §7: an owner_action or decision hold parks the row until a
    human signal releases it — there is no timer to wait out."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(hold_kind="owner_action"), **kw) == "held"
    assert skip_reason(_row(hold_kind="decision"), **kw) == "held"
    # the two auto-exiting kinds are eligible
    assert skip_reason(_row(hold_kind="evidence"), **kw) is None
    assert skip_reason(_row(hold_kind="stronger_model"), **kw) is None


def test_needs_human_label_alone_never_skips():
    """Phase D: the label is not a signal; only the hold is."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(labels=("needs-human",), gh_updated_at=NOW), **kw) is None


def test_held_outranks_the_board_status_it_causes():
    """The reconciler stamps ``Held`` on exactly these rows, so a status-first
    check would report them as plain "status" skips and the card would lose its
    held gauge."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status="Held", hold_kind="owner_action"), **kw) == "held"


def test_the_held_column_is_a_status_skip():
    """A card sitting in the Held column must not be dispatched."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status="Held"), **kw) == "status"


def test_needs_spec_skips_a_bounced_row_before_the_status_gate():
    """D8: a row that keeps bouncing on the same body is parked as ``needs_spec``
    — checked immediately after the held check and before the status gate, so it
    is reported whatever the board's Status column says."""
    kw = dict(min_status=STATUS_READY, respect_dependencies=True, now=NOW)
    row = _row(bounces=2, status=STATUS_READY)
    assert skip_reason(row, give_up_after_bounces=2, **kw) == "needs_spec"
    assert skip_reason(row, give_up_after_bounces=3, **kw) is None
    assert skip_reason(row, give_up_after_bounces=0, **kw) is None  # 0 = never
    assert "needs_spec" in SKIP_REASONS


def test_rank_counts_needs_spec():
    rows = [_row(ref="o/r#1", number=1, bounces=2), _row(ref="o/r#2", number=2, bounces=0)]
    entries, skipped = rank(rows, min_status=STATUS_READY, respect_dependencies=True,
                            now=NOW, give_up_after_bounces=2)
    assert skipped == {"needs_spec": 1}
    assert [e.number for e in entries] == [2]


def test_rank_carries_hold_kind_onto_entries():
    entries, skipped = rank([_row(hold_kind="evidence"), _row(ref="o/r#2", number=2, hold_kind="owner_action")],
                            min_status="Backlog", respect_dependencies=True, now=NOW)
    assert [(e.ref, e.hold_kind) for e in entries] == [("o/r#1", "evidence")]
    assert skipped == {"held": 1}
    assert "held" in SKIP_REASONS and "needs_human_cooldown" not in SKIP_REASONS


def test_rank_orders_score_desc_nulls_last_then_age_and_counts_skips():
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", open_blockers=("o/r#4",)),
            _row(ref="o/r#6", status="Backlog")]
    entries, skipped = rank(rows, now=NOW)
    assert [e.ref for e in entries] == ["o/r#4", "o/r#3", "o/r#1", "o/r#2"]
    assert skipped == {"blocked": 1, "status": 1}


def test_rank_key_is_the_one_ordering_and_applies_to_rows_and_entries():
    """``rank`` is not always the last word on ordering: a caller that ranks each
    product separately (the Backlog Runner, whose products have their own reach)
    has to merge, and that merge must sort by exactly this key rather than a
    hand-copied tuple that can drift from it. So the key is exported, and it takes
    a ``QueueRow`` before ranking and a ``QueueEntry`` after it."""
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", score=None, gh_created_at=None)]
    assert [r.ref for r in sorted(rows, key=rank_key)] == [
        "o/r#4", "o/r#3", "o/r#1", "o/r#2", "o/r#5"]

    # The same key over ENTRIES reproduces rank()'s own order — which is what
    # makes a cross-product merge and a single rank() agree.
    entries, _ = rank(rows, now=NOW)
    assert [e.ref for e in sorted(entries, key=rank_key)] == [e.ref for e in entries]

    # An undated row sorts last on its own boolean leg rather than via a sentinel
    # datetime, so the key never compares a naive datetime with an aware one.
    undated = _row(ref="o/r#9", score=5.0, gh_created_at=None)
    # Index 3, not 2: urgency is a leading leg of the key (see the incident tests
    # at the bottom of this file).
    assert rank_key(undated)[3] is True
    assert sorted([undated, rows[0]], key=rank_key)[0].ref == "o/r#1"


def test_to_candidate_is_the_runner_shape_plus_provenance():
    entries, _ = rank([_row()], now=NOW)
    assert to_candidate(entries[0]) == {"ref": "o/r#1", "number": 1, "repo": "o/r", "title": "t",
                                        "body": "", "url": "u", "score": 10.0, "product": "viberpit",
                                        "urgent": False, "hold_kind": None}


def test_to_candidate_carries_urgency_to_the_grouper():
    # The grouper re-orders the pool into a plan, so urgency has to survive the
    # hand-off as DATA — the candidate dict is the only thing it receives.
    entries, _ = rank([_row(labels=("deploy-monitor",))], now=NOW)
    assert to_candidate(entries[0])["urgent"] is True


def test_rank_rejects_unknown_min_status():
    kw = dict(respect_dependencies=True, now=NOW)
    with pytest.raises(ValueError, match="min_status must be one of"):
        skip_reason(_row(), min_status="Nope", **kw)
    with pytest.raises(ValueError, match="min_status must be one of"):
        rank([_row()], min_status="Nope", now=NOW)


def test_min_status_options_are_the_first_two_board_statuses():
    # gh_reconcile._ensure_fields seeds the board's Status field options in
    # STATUS_OPTIONS display order (Backlog, Ready, In progress, Blocked, Held,
    # Done). The queue's min_status gate only ever admits from the front
    # of that order — pin the coupling so a reorder of STATUS_OPTIONS is caught
    # here instead of silently changing what "Backlog" or "Ready" mode means.
    assert MIN_STATUS_OPTIONS == STATUS_OPTIONS[:2]


def test_backlog_mode_admits_unstamped_and_foreign_statuses():
    # Decision C-2: an item the reconciler never stamped (status=None) or one
    # sitting in a non-spec column (GitHub's default "Todo") is Backlog-equivalent
    # so an unreconciled board doesn't yield an empty queue.
    kw = dict(respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status=None), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Todo"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status=None), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Todo"), min_status="Ready", **kw) == "status"


def test_every_skip_reason_is_in_the_vocabulary():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW,
             give_up_after_bounces=2)
    cases = {
        "status": _row(status="Done"),
        "blocked": _row(status="Blocked"),
        "live_session": _row(live_session=True),
        "claimed": _row(claimed=True),
        "assigned": _row(assignees=("steiner385",)),
        "epic_open": _row(open_children=1),
        "bot": _row(author="renovate[bot]"),
        "untrusted": _row(author="stranger"),
        "held": _row(hold_kind="owner_action"),
        "needs_spec": _row(bounces=2),
    }
    seen = set()
    for expected, row in cases.items():
        got = skip_reason(row, **kw)
        assert got == expected
        seen.add(got)
    assert seen == set(SKIP_REASONS)


# ── urgency: a reported incident preempts the queue (#16150, #16151) ──────────

def test_a_reported_incident_outranks_everything_regardless_of_score_and_age():
    """The incident the monitor files is unscored and brand new — which under a
    score-then-oldest key is the WORST possible position, not the best.

    Live evidence: cairnea/KinDash#16150 and #16151 reported both admin
    environments down and ranked 328th and 329th of 332, behind three-day-old
    Playwright flakes, for the seven hours the environments stayed broken.
    Urgency is therefore a LEADING leg of the key: no score and no age can put a
    downed environment behind ordinary backlog.
    """
    incident = _row(ref="o/r#900", score=None, labels=("deploy-monitor",),
                    gh_created_at=NOW)  # unscored AND newest: dead last before this
    scored_old = _row(ref="o/r#1", score=9.0, gh_created_at=NOW - timedelta(days=30))
    plain = _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=1))
    assert [r.ref for r in sorted([scored_old, plain, incident], key=rank_key)] == [
        "o/r#900", "o/r#1", "o/r#2"]


def test_urgency_is_case_insensitive_like_the_bot_label_check():
    assert rank_key(_row(labels=("Deploy-Monitor",)))[0] is False


def test_urgency_survives_the_row_to_entry_merge():
    """``rank_key`` must answer the same before and after ranking: the Backlog
    Runner ranks each product separately and re-sorts the MERGED entries. Urgency
    living only on the row would be silently dropped by exactly that merge."""
    rows = [_row(ref="o/r#1", score=9.0, gh_created_at=NOW - timedelta(days=30)),
            _row(ref="o/r#900", score=None, labels=("deploy-monitor",), gh_created_at=NOW)]
    entries, _ = rank(rows, now=NOW)
    assert [e.ref for e in entries] == ["o/r#900", "o/r#1"]
    assert (entries[0].urgent, entries[1].urgent) == (True, False)
    assert [e.ref for e in sorted(entries, key=rank_key)] == [e.ref for e in entries]


def test_urgent_labels_pin_the_deploy_monitor_label():
    # Same pinning rationale as MIN_STATUS_OPTIONS above: the label is OWNED by
    # the automation that files the issue, but importing an automation into the
    # pure ranking core would invert the layering. Pin the coupling here so
    # renaming the automation's LABEL fails this test instead of silently
    # demoting every incident back to the bottom of the queue.
    from server.automations.deploy_monitor import LABEL

    assert LABEL in URGENT_LABELS


def test_ordinary_ranking_is_unchanged_by_the_urgency_leg():
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", score=None, gh_created_at=None)]
    assert [r.ref for r in sorted(rows, key=rank_key)] == [
        "o/r#4", "o/r#3", "o/r#1", "o/r#2", "o/r#5"]


def test_is_bot_noise_is_the_predicate_behind_the_bot_skip():
    """The public predicate ``skip_reason`` applies for its ``bot`` verdict — exported
    so the hold-hygiene pass and the approve-escalation tap judge an issue by the
    SAME rule (cairnea/KinDash#16652), not a copy of it."""
    from server.pm.queue import is_bot_noise
    assert is_bot_noise(author="renovate[bot]", labels=(), title="t")
    assert is_bot_noise(author="tony", labels=("Dependencies",), title="t")
    assert is_bot_noise(author="tony", labels=(), title="Dependency Dashboard")
    assert is_bot_noise(author="tony", labels=("On-Hold",), title="t",
                        extra_skip_labels=frozenset({"on-hold"}))
    assert not is_bot_noise(author="tony", labels=("on-hold",), title="t")
    # A GraphQL-mirrored login carries no ``[bot]`` suffix (``github-actions``, not
    # ``github-actions[bot]``), so the author leg deliberately does not fire on it:
    # a per-test failure issue the same workflow files IS work. Labels decide.
    assert not is_bot_noise(author="github-actions", labels=(), title="t")
    assert is_bot_noise(author="github-actions", labels=("forced-deploy-audit",), title="t",
                        extra_skip_labels=frozenset({"forced-deploy-audit"}))


TRUSTED = frozenset({"steiner385", "app/viberpit"})


def test_untrusted_author_is_skipped_before_dispatch():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW, trusted_authors=TRUSTED)
    assert skip_reason(_row(author="steiner385"), **kw) is None
    assert skip_reason(_row(author="stranger"), **kw) == "untrusted"


def test_trust_is_case_insensitive_and_ignores_app_prefix_and_bot_suffix_forms():
    assert is_trusted_author("Steiner385", TRUSTED)
    assert is_trusted_author("app/viberpit", TRUSTED)
    assert is_trusted_author("App/Viberpit", TRUSTED)
    assert is_trusted_author("viberpit[bot]", TRUSTED)


def test_a_user_squatting_an_app_login_is_untrusted():
    """The mirror stores a Bot as ``app/<slug>``; a bare login is a USER, and anyone can
    register the user ``viberpit`` or ``github-actions`` and open an issue."""
    assert not is_trusted_author("viberpit", TRUSTED)
    assert not is_trusted_author("github-actions", frozenset())
    assert not is_trusted_author("github-actions", TRUSTED)


def test_a_user_is_trusted_only_by_a_bare_listing():
    assert is_trusted_author("steiner385", TRUSTED)
    assert not is_trusted_author("app/steiner385", TRUSTED)
    assert not is_trusted_author("app/stranger", TRUSTED)


def test_automation_logins_are_always_trusted():
    assert is_trusted_author("app/github-actions", frozenset())
    assert is_trusted_author("github-actions[bot]", frozenset())


def test_ghost_author_is_untrusted():
    assert not is_trusted_author("", TRUSTED)
    assert not is_trusted_author("ghost", TRUSTED)


def test_empty_trusted_list_fails_closed_for_humans():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW,
              trusted_authors=frozenset())
    assert skip_reason(_row(author="steiner385"), **kw) == "untrusted"


def test_untrusted_is_a_declared_skip_reason():
    assert "untrusted" in SKIP_REASONS


def test_trusted_automation_logins_pin_alert_series():
    from server.pm.alert_series import AUTOMATION_LOGINS
    from server.pm.queue import TRUSTED_AUTOMATION_LOGINS
    assert TRUSTED_AUTOMATION_LOGINS == AUTOMATION_LOGINS


def test_alert_series_recognises_the_mirrors_app_form():
    from server.pm.alert_series import alert_key, is_automation_login
    assert is_automation_login("app/github-actions")
    assert not is_automation_login("github-actions")      # a bare login is a user
    assert is_automation_login("renovate[bot]")
    assert not is_automation_login("app/tony")
    assert alert_key("Weekly failed — 2026-09-01", "app/github-actions") == \
        alert_key("Weekly failed — 2026-09-01", "github-actions[bot]")
    assert alert_key("Weekly failed — 2026-09-01", "github-actions") is None
    assert alert_key("Weekly failed — 2026-09-01", "app/github-actions") is not None


def test_a_bare_github_actions_user_cannot_supersede_a_real_alert_series():
    from server.pm.alert_series import AlertIssue, superseded
    title = "⚠️ Unverified prod deploy b80aa1b03c — 2026-09-21"
    real = AlertIssue("o/r", 10, title, "app/github-actions")
    rest = AlertIssue("o/r", 11, title.replace("2026-09-21", "2026-09-22"), "github-actions[bot]")
    squat = AlertIssue("o/r", 99, title.replace("2026-09-21", "2026-09-30"), "github-actions")
    assert superseded([real, squat]) == []
    assert superseded([real, rest, squat]) == [("o/r#10", "o/r#11")]
