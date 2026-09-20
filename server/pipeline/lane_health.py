"""Per-repo Delivery-spine lane facts the server can vouch for
(plotroom ``DashboardState.repos[].laneHealth`` / ``.deploy``).

``lane_health_ctx`` populates ``main``, and only for the repo the deploy
controller watches. Its tip verdict (``steps.ci_verdict`` on ``origin/master``,
cached by the detect loop) is the ONE reading of "is master in CI": the deploy
banner's "CI pending" and the Pipeline's main lane must come from that same
reading, or they disagree exactly when an operator is deciding whether to
deploy. On 2026-09-11 the banner said "CI pending" for #1546 while the
Pipeline said "main · no signal" — the lane had never been wired to anything.

``controller_deploy_snapshot`` fills the same repo's ``deploy`` lane from the
same ``/status`` payload (#1548) — same shape of bug, same attribution.
"""
from __future__ import annotations

# ci_verdict vocabulary (server/deploy/steps.py) → LaneStatus (web/src/pipeline/types.ts).
# "pending" and "unrun" are watch-states: the tip is not known-broken, but nothing
# should deploy on it yet. "unavailable" (and anything unknown) is blind, never a
# false green — a lane that reads green while the verdict is unreadable would let
# the header roll up to "all clear" with master actually mid-run.
_VERDICT_TO_LANE = {
    "green": "green",
    "red": "red",
    "pending": "amber",
    "unrun": "amber",
    "unavailable": "blind",
}


def main_lane_health(pending: dict) -> dict:
    """``laneHealth`` for one repo from the controller's ``pending`` block.

    ``tipCi`` is the tip's own verdict; pre-green-frontier controllers shipped only
    ``ci``, which then equalled it. ``ci`` alone can read "green" for a red tip
    (an older pending commit is deployable), so it is only a fallback.
    """
    verdict = str(pending.get("tipCi") or pending.get("ci") or "unavailable")
    return {
        "main": _VERDICT_TO_LANE.get(verdict, "blind"),
        "tipSha": pending.get("targetSha") or None,
        "tipCi": verdict,
    }


def lane_health_ctx(status: dict | None) -> dict[str, dict]:
    """``{repo: laneHealth}`` from one ``/status`` payload of the deploy controller.

    The controller names the checkout it watches (``repo`` = origin slug). Without
    that name the verdict cannot be attributed to a repo, so an older controller
    (or an unreachable one → ``None``) yields no claim at all — the lane stays
    blind rather than pinning master's verdict on the wrong repository.
    """
    if not status:
        return {}
    repo = status.get("repo")
    pending = status.get("pending")
    if not repo or not isinstance(pending, dict):
        return {}
    return {str(repo): main_lane_health(pending)}


def controller_deploy_snapshot(status: dict | None) -> dict[str, dict]:
    """``{repo: deploy}`` synthesized for the deploy controller's own repo (#1548).

    ``server/api/pipeline.py:_deploy_ctx`` only builds a ``deploy`` block for repos
    with a ``RepoDeployConfig`` (environment health URLs the env watcher polls).
    ViberPit's own rollout goes through this deploy controller instead — it has no
    such config — so its Deploy lane read "not wired" while the banner above it was
    offering "Deploy now" for the same repo. This mirrors ``lane_health_ctx``'s
    attribution (``status.repo``) to turn the controller's own ``pending`` snapshot
    into one advisory env entry: "prod-edge" is live at ``liveSha`` (the running
    edge answered, so it counts as reachable), with ``awaitingProd`` the commit
    count a deploy right now would ship — ``deployableCount`` when the tip (or an
    older green frontier) is deployable, else every pending commit.
    """
    if not status:
        return {}
    repo = status.get("repo")
    pending = status.get("pending")
    if not repo or not isinstance(pending, dict):
        return {}
    commits = pending.get("commits")
    awaiting_prod = pending.get("deployableCount") or (
        len(commits) if isinstance(commits, list) else 0)
    return {str(repo): {
        "envs": [{"name": "prod-edge", "liveSha": pending.get("liveSha") or None,
                  "reachable": True}],
        "awaitingQa": 0,
        "awaitingProd": awaiting_prod,
    }}
