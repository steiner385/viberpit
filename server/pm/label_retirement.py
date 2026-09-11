"""Pure planning core of the ONE-TIME claim-label retirement (Phase D, #994).

Phase D retired the ``in-progress`` / ``needs-human`` labels as machine state: a
claim is the runner's durable target-state row and a give-up is its ``gave_up``
row, which the reconciler turns into board Status ``Needs human``. The labels
already hanging on open issues are the leftovers, and ``scripts/retire_claim_labels.py``
is the sweep that clears them once, after deploy.

The planning half lives here, apart from that script's ``gh`` shell, for two
reasons: ``scripts/`` is not on the import path (a test must load that file by
path, as ``server/tests/test_retire_claim_labels.py`` does for its apply
helpers), and the decision this
makes — whose signal is this label? — is exactly the part that must be reviewable
before it runs. ``plan_actions`` is pure over canned ``gh`` output, so the dry run
an operator reads and the ``--apply`` run are the same computation.

Decision D-3, on who placed the ``needs-human`` label (latest ``labeled`` event):

* a ``[bot]`` labeler is the runner announcing its own give-up → a ``gave_up``
  row dated by the label event, so the queue's cooldown and the reconciler's
  Status both pick it up from the one place Phase D reads;
* anyone else (or no timeline evidence at all) is a human's signal → a human
  Status edit to ``Needs human`` on the product's board, which the reconciler
  treats as a human decision and never overwrites;
* ``in-progress`` is always just stripped — a stale claim's only durable trace
  was the label, and the runner no longer reads it.

The label *definitions* are not this module's business: ``needs-human`` stays in
every repo (humans may keep using it), ``in-progress`` is deleted by hand after
the sweep (runbook).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# THE row-timestamp parser (re-exported by ``server.pm.queue_store`` as
# ``parse_iso_utc``, which is what the queue and the runner import). Taken from
# its home module here so this planner stays pure — importing it through
# ``queue_store`` would pull the ORM, the mirror and the automation registry into
# a script that only needs to read a date.
from server.time_iso import parse_iso_utc

# The two retired label names, as literals, HERE — Phase D deleted them from
# ``server/pm/queue.py`` because no runtime path may read them again. This
# one-shot migration is the last consumer, and it must not resurrect them where
# live code could pick them back up.
IN_PROGRESS = "in-progress"
NEEDS_HUMAN = "needs-human"

ActionKind = Literal["strip_in_progress", "row_from_bot_label", "status_from_human_label"]


@dataclass(frozen=True)
class Action:
    """One planned mutation. ``at`` is the ISO-8601 UTC timestamp of the latest
    ``labeled`` event for that label (the ``gave_up`` row's ``needs_human_at``),
    ``actor`` its labeler login; both are ``None`` when the timeline has no such
    event."""

    kind: ActionKind
    ref: str
    at: str | None
    actor: str | None


def _is_bot(actor: str | None) -> bool:
    """GitHub suffixes every App/bot login with ``[bot]`` — that suffix is the
    only attribution the timeline gives us, and it is enough: the runner comments
    and labels as its App, humans never can (the suffix is reserved)."""
    return bool(actor) and str(actor).endswith("[bot]")


def _latest_labeled(timeline: Iterable[Mapping], label: str) -> tuple[str | None, str | None]:
    """``(at, actor)`` of the newest ``labeled`` event for *label*, or ``(None, None)``.

    Newest, not last: a label can be removed and re-applied, and the live signal
    is whoever applied it most recently. Events with an unparseable date lose to
    any dated one but still beat nothing, so a malformed stamp cannot erase the
    attribution that decides bot-vs-human.
    """
    best: tuple[datetime | None, str | None, str | None] | None = None
    for ev in timeline or ():
        if ev.get("event") != "labeled":
            continue
        if ((ev.get("label") or {}).get("name")) != label:
            continue
        raw = ev.get("created_at")
        # Round-tripped through the shared parser (which reads GitHub's trailing
        # ``Z``) so the string a ``gave_up`` row carries is written in exactly the
        # offset form ``parse_iso_utc`` reads back.
        when = parse_iso_utc(str(raw) if raw else None)
        actor = (ev.get("actor") or {}).get("login")
        at = when.isoformat() if when else None
        if best is None or (when is not None and (best[0] is None or when >= best[0])):
            best = (when, at, actor)
    return (best[1], best[2]) if best else (None, None)


def plan_actions(repo: str, open_issues: Sequence[Mapping],
                 timelines: Mapping[int, Sequence[Mapping]]) -> list[Action]:
    """The full plan for one repo, in issue order; ``needs-human`` before
    ``in-progress`` on an issue wearing both, so the issue's durable replacement
    exists before its claim label goes away.

    No filtering happens here — ``--only`` is the shell's, so the reviewed dry run
    and the applied run are the same computation.
    """
    actions: list[Action] = []
    for issue in open_issues:
        number = int(issue.get("number") or 0)
        if not number:
            continue
        ref = f"{repo}#{number}"
        names = {(lb or {}).get("name") for lb in (issue.get("labels") or ())}
        timeline = timelines.get(number) or ()
        if NEEDS_HUMAN in names:
            at, actor = _latest_labeled(timeline, NEEDS_HUMAN)
            kind: ActionKind = (
                "row_from_bot_label" if _is_bot(actor) else "status_from_human_label")
            actions.append(Action(kind, ref, at, actor))
        if IN_PROGRESS in names:
            at, actor = _latest_labeled(timeline, IN_PROGRESS)
            actions.append(Action("strip_in_progress", ref, at, actor))
    return actions
