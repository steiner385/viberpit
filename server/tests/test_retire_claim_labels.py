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
import asyncio
import importlib.util
from pathlib import Path

import pytest

from server.pm.label_retirement import Action, plan_actions

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "retire_claim_labels.py"
_SPEC = importlib.util.spec_from_file_location("retire_claim_labels", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
retire = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retire)


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


# --------------------------------------------------------------------------- #
# Live-claim guard (shared by both replacement paths)                          #
# --------------------------------------------------------------------------- #

def _state_of(row: dict):
    """A fake target-state lookup — the guard's only I/O, so both apply helpers
    are exercised with no database and no ``gh``."""
    async def get_state(_ref: str) -> dict:
        return row
    return get_state


def test_human_path_skips_while_the_runner_holds_a_live_claim():
    """A stale ``needs-human`` label can outlive the deploy and the runner then
    re-claim the issue. Writing board Status ``Needs human`` under that live
    session would be overwritten with ``In progress`` by the reconciler on its
    next tick — so the human's signal would be lost WITH the label stripped. The
    human path must SKIP exactly as the bot path does."""
    action = Action(kind="status_from_human_label", ref="o/r#7", at=None, actor="steiner385")
    live = {"claimed": True, "session_id": "s1", "issues": ["o/r#7"]}
    with pytest.raises(LookupError, match="live claim"):
        asyncio.run(retire.set_board_needs_human(action, "tok", get_state=_state_of(live)))
    # Same guard, same sentinel, on the bot path.
    bot = Action(kind="row_from_bot_label", ref="o/r#7", at="2026-09-01T00:00:00+00:00",
                 actor="viberpit[bot]")
    with pytest.raises(LookupError, match="live claim"):
        asyncio.run(retire.write_gave_up_row(bot, get_state=_state_of(live)))


def test_a_gave_up_row_is_not_a_live_claim():
    """Phase D: a ``gave_up`` row replaces the claim, so it must never read as
    live — the label on top of it is simply stale."""
    row = {"gave_up": True, "claimed": True, "needs_human_at": "2026-09-01T00:00:00+00:00"}
    assert asyncio.run(
        retire.state_unless_live_claim("o/r#7", get_state=_state_of(row))) == row
    assert asyncio.run(
        retire.state_unless_live_claim("o/r#7", get_state=_state_of({}))) == {}
