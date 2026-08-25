"""Pure Backlog Runner helpers (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from server.github.pr_status import PullRequestInfo

# The user-selectable per-repo selection strategies (see ``order_live_groups``).
# ``balanced`` is the default and reproduces the pre-strategy behavior.
SELECTION_STRATEGIES = ("balanced", "priority", "weighted", "concurrency-cap")


@dataclass(frozen=True)
class Batch:
    """One unit of work the runner spawns a session for: a primary issue key
    (``"owner/repo#123"``) plus the issues bundled into its PR and the repo
    they belong to.  This is the contract the injected ``select_batch`` seam
    returns; the loop reads ``batch.primary`` / ``batch.issues``."""

    primary: str
    issues: list[str] = field(default_factory=list)
    repo: str = ""
    # ref → issue title for the batched issues; feeds the "auto" model
    # assessor's job summary (refs alone carry no complexity signal).
    titles: dict[str, str] = field(default_factory=dict)


def should_free_slot(pr_info: PullRequestInfo, pr_doctor_active: bool) -> bool:
    """A runner slot frees when the work item's PR has reached the point where
    something else carries it: PR Doctor keeps CI green toward auto-merge, so if
    PR Doctor covers the repo, free once the PR is open AND auto-merge is armed;
    otherwise (no safety net) hold until the PR actually merges."""
    if pr_info.state == "merged":
        return True
    if pr_doctor_active:
        return pr_info.state in ("open", "draft") and pr_info.auto_merge_enabled
    return False


def order_live_groups(
    items: list,
    *,
    repo_of: Callable[[Any], str],
    strategy: str = "balanced",
    repo_load: "dict[str, int] | None" = None,
    repo_order: "list[str] | None" = None,
    weights: "dict[str, float] | None" = None,
    caps: "dict[str, int] | None" = None,
) -> list:
    """Reorder (and, for ``concurrency-cap``/``weighted``, filter) the live plan
    groups *items* by the configured per-repo selection strategy. The caller then
    pops ``result[0]``; an empty result means "idle this slot" (every candidate
    repo was filtered out).

    Pure and STABLE: within an equal sort key the incoming plan-priority order is
    preserved, so a strategy never scrambles rank inside a single repo.
    ``repo_of`` maps an item to its repo. ``repo_load`` is the per-repo active
    pipeline count (``_backlog_repo_load``); a repo absent from it counts as 0.

    Strategies (see ``SELECTION_STRATEGIES``):

    * ``balanced`` (default) — fewest in-pipeline runs first: equal rotation among
      repos that have work. An empty ``repo_load`` leaves plan order untouched
      (this reproduces the pre-strategy behavior exactly).
    * ``priority`` — repos are served in ``repo_order`` order; a repo not listed
      ranks last. ``repo_load`` breaks ties *within* a rank. A deep top-rank
      backlog can legitimately starve lower repos — that is the point of the mode.
    * ``weighted`` — effective load = ``load / weight`` (``weights`` default 1), so
      a higher weight wins more freed slots. A weight of 0 (or less) SUPPRESSES the
      repo entirely — a "pause this repo" knob — dropping it from selection.
    * ``concurrency-cap`` — a repo whose active load is already ``>=`` its cap
      (``caps``; a repo absent from the map is uncapped) is dropped for this slot;
      the survivors are ordered ``balanced``. An empty result means every repo with
      work is at its cap, so the caller idles rather than exceeding a hard ceiling.
    """
    load = repo_load or {}

    if strategy == "priority":
        order = repo_order or []
        rank = {r: i for i, r in enumerate(order)}
        return sorted(items, key=lambda it: (rank.get(repo_of(it), len(order)),
                                             load.get(repo_of(it), 0)))

    if strategy == "weighted":
        w = weights or {}
        kept = [it for it in items if w.get(repo_of(it), 1) > 0]
        return sorted(kept, key=lambda it: load.get(repo_of(it), 0)
                      / w.get(repo_of(it), 1))

    if strategy == "concurrency-cap":
        c = caps or {}
        kept = [it for it in items
                if (cap := c.get(repo_of(it))) is None or load.get(repo_of(it), 0) < cap]
        return sorted(kept, key=lambda it: load.get(repo_of(it), 0))

    # balanced (default). Empty load → identity (stable), preserving plan order.
    if not load:
        return list(items)
    return sorted(items, key=lambda it: load.get(repo_of(it), 0))
