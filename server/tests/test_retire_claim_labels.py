"""Planning core of the one-time Phase D claim-label retirement (#994).

The planner is pure so the sweep can be reviewed before it runs: the operator sees
every action the migration would take (`--apply` withheld) against canned `gh`
output, and the classification that decides between a runner row and a human's
board Status is unit-tested rather than discovered in production.

Decision D-3: a `needs-human` label placed BY THE RUNNER (a `[bot]` labeler) is the
runner's own handoff, so it becomes a `gave_up` row dated by the label event; one
placed by a human is a human's signal, so it becomes a board Status edit the
reconciler never overwrites. `in-progress` is always just stripped.
"""
from server.pm.label_retirement import plan_actions


def test_plan_actions_classifies_labels_by_actor():
    issues = [{"number": 1, "labels": [{"name": "in-progress"}]},
              {"number": 2, "labels": [{"name": "needs-human"}]},
              {"number": 3, "labels": [{"name": "needs-human"}, {"name": "in-progress"}]}]
    timelines = {
        2: [{"event": "labeled", "label": {"name": "needs-human"},
             "actor": {"login": "viberpit[bot]"}, "created_at": "2026-09-01T00:00:00Z"}],
        3: [{"event": "labeled", "label": {"name": "needs-human"},
             "actor": {"login": "steiner385"}, "created_at": "2026-09-02T00:00:00Z"}],
    }
    kinds = [(a.kind, a.ref) for a in plan_actions("o/r", issues, timelines)]
    assert kinds == [("strip_in_progress", "o/r#1"),
                     ("row_from_bot_label", "o/r#2"),
                     ("status_from_human_label", "o/r#3"), ("strip_in_progress", "o/r#3")]
    at = next(a for a in plan_actions("o/r", issues, timelines) if a.ref == "o/r#2").at
    assert at == "2026-09-01T00:00:00+00:00"  # the row's needs_human_at, Z normalised


def test_needs_human_with_no_timeline_event_is_treated_as_human():
    """No evidence of who labelled it ⇒ human. The safe direction: a Status edit
    asks a person, while a bot row would silently re-enter the issue into the
    runner's retry cycle on a signal nobody can attribute."""
    issues = [{"number": 7, "labels": [{"name": "needs-human"}]}]
    actions = plan_actions("o/r", issues, {})
    assert [(a.kind, a.ref, a.at, a.actor) for a in actions] == [
        ("status_from_human_label", "o/r#7", None, None)]


def test_latest_labeled_event_decides_actor_and_timestamp():
    """A label can be removed and re-applied; the LAST application is the live
    signal, so a bot re-label after a human's (or vice versa) decides both the
    classification and the date, whatever order the timeline arrives in."""
    issues = [{"number": 5, "labels": [{"name": "needs-human"}]}]
    timelines = {5: [
        {"event": "labeled", "label": {"name": "needs-human"},
         "actor": {"login": "viberpit[bot]"}, "created_at": "2026-09-05T10:00:00+00:00"},
        {"event": "unlabeled", "label": {"name": "needs-human"},
         "actor": {"login": "steiner385"}, "created_at": "2026-09-03T00:00:00Z"},
        {"event": "labeled", "label": {"name": "needs-human"},
         "actor": {"login": "steiner385"}, "created_at": "2026-09-02T00:00:00Z"},
        {"event": "labeled", "label": {"name": "in-progress"},
         "actor": {"login": "someone"}, "created_at": "2026-09-09T00:00:00Z"},
    ]}
    (action,) = plan_actions("o/r", issues, timelines)
    assert (action.kind, action.actor, action.at) == (
        "row_from_bot_label", "viberpit[bot]", "2026-09-05T10:00:00+00:00")


def test_plan_actions_plans_every_label_and_leaves_filtering_to_the_shell():
    """``--only`` is the I/O shell's filter, not the core's: the planner always
    returns the full plan, so the dry run an operator reviews and the `--apply`
    run are the same computation with a `kind` filter in front of it."""
    issues = [{"number": 1, "labels": [{"name": "in-progress"}, {"name": "bug"}]},
              {"number": 2, "labels": [{"name": "needs-human"}]},
              {"number": 3, "labels": [{"name": "enhancement"}]}]
    actions = plan_actions("o/r", issues, {})
    assert [a.kind for a in actions] == ["strip_in_progress", "status_from_human_label"]
    only = [a for a in actions if a.kind == "strip_in_progress"]
    assert [a.ref for a in only] == ["o/r#1"]
