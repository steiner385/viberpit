"""Minimal frame-diffing notifier (spec §8 phase 2): ci-failed / ready / overdue.

Fires only on inactive→active transitions; re-fires only after the condition
clears for a frame. Rendered strings mirror plotroom notifier.ts renderNotification.
"""
from server.pipeline.notifier import TransitionNotifier


def _frame(substate=None, stage="ci", overdue=False, number=7):
    return {"generatedAt": "t", "staleSince": None, "repos": [{
        "repo": "o/r", "hasDeploy": False, "queue": None, "prs": [{
            "repo": "o/r", "number": number, "title": "fix auth", "url": "u",
            "stage": {"stage": stage, "substate": substate, "percent": None,
                      "etaSeconds": None, "etaRangeSeconds": None, "overdue": overdue},
        }]}]}


def test_fires_on_new_ci_failed_only_once():
    n = TransitionNotifier()
    assert n.diff(_frame()) == []                       # baseline frame
    events = n.diff(_frame(substate="ci-failed", stage="parked"))
    assert [e["type"] for e in events] == ["ci-failed"]
    assert events[0]["rendered"] == {"title": "o/r#7 CI failed", "body": "fix auth"}
    assert n.diff(_frame(substate="ci-failed", stage="parked")) == []   # still active → no refire


def test_refires_after_condition_clears():
    n = TransitionNotifier()
    n.diff(_frame())
    n.diff(_frame(substate="ci-failed", stage="parked"))
    n.diff(_frame())                                    # cleared
    assert [e["type"] for e in n.diff(_frame(substate="ci-failed", stage="parked"))] == ["ci-failed"]


def test_ready_and_overdue():
    n = TransitionNotifier()
    n.diff(_frame())
    ev = n.diff(_frame(stage="ready"))
    assert [e["type"] for e in ev] == ["ready"]
    assert ev[0]["rendered"]["title"] == "o/r#7 ready to merge"
    ev2 = n.diff(_frame(stage="ready", overdue=True))
    assert [e["type"] for e in ev2] == ["overdue"]


def test_first_frame_is_baseline_never_fires():
    n = TransitionNotifier()
    assert n.diff(_frame(substate="ci-failed", stage="parked")) == []
