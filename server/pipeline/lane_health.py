"""Per-repo Delivery-spine lane facts the server can vouch for
(plotroom ``DashboardState.repos[].laneHealth``).

Only ``main`` is populated today, and only for the repo the deploy controller
watches. Its tip verdict (``steps.ci_verdict`` on ``origin/master``, cached by the
detect loop) is the ONE reading of "is master in CI": the deploy banner's
"CI pending" and the Pipeline's main lane must come from that same reading, or
they disagree exactly when an operator is deciding whether to deploy. On
2026-09-11 the banner said "CI pending" for #1546 while the Pipeline said
"main · no signal" — the lane had never been wired to anything.
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
