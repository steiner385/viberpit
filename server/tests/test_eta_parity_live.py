"""Live parity gate: ccswitch ETA matches oracle within tolerance.

This test fetches the live Plotroom oracle state (http://127.0.0.1:4400/api/state),
then for each PR the oracle classifies as stage=ci with a non-null etaSeconds it
re-runs our ETA engine using the oracle's own check snapshot as input.  Using
oracle state rather than a fresh GitHub query eliminates state-lag as a source of
mismatch: we're feeding the SAME data to our algorithm as the oracle fed to its own.

Excluded stages (documented reasons):
  - queue:    ETA comes from queue.ts (multi-PR simulation), not CI-progress math.
  - qa-deploy: ETA comes from deployment-propagation tracking, not CI checks.
  Only stage='ci' PRs exercise the same compute_pr_eta code path as the oracle.

Tolerance: abs(ours - oracle) <= max(2, 0.05 * oracle_eta) per-PR.
"""
import json, os, urllib.request
from datetime import datetime, timezone
import pytest

ORACLE = "http://127.0.0.1:4400/api/state"
SOURCE = "/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db"


def _oracle_state():
    try:
        with urllib.request.urlopen(ORACLE, timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


def _oracle_check_to_ours(c: dict, state_at: datetime) -> dict:
    """Convert an oracle check (elapsedSeconds-based) to our format.

    The oracle exposes ``elapsedSeconds`` rather than ``startedAt``; we
    reconstruct a ``started_at`` ISO string from the oracle snapshot time.
    The ``event`` field is always ``pull_request`` — oracle only surfaces
    PR-event checks in the API state (merge_group checks are separate).
    """
    elapsed = c.get("elapsedSeconds") or 0
    if c.get("status") == "IN_PROGRESS":
        started_s = state_at.timestamp() - elapsed
        started_at = (
            datetime.fromtimestamp(started_s, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    else:
        started_at = None
    return {
        "name": c["name"],
        "status": c["status"],
        "conclusion": c.get("conclusion"),
        "started_at": started_at,
        "completed_at": None,   # not needed: COMPLETED checks contribute progress=1
        "isRequired": bool(c.get("isRequired")),
        "workflowName": c.get("workflowName"),
        "event": "pull_request",
    }


@pytest.mark.skipif(not os.path.exists(SOURCE), reason="history.db source absent")
def test_eta_live_parity():
    state = _oracle_state()
    if state is None:
        pytest.skip("live Plotroom oracle not reachable")

    from server.pipeline.history_migrate import migrate_history
    from server.pipeline.history import HistoryStore
    from server.pipeline.eta import compute_pr_eta

    migrate_history(SOURCE, "/tmp/parity_history.db")
    store = HistoryStore("/tmp/parity_history.db")

    # Use the oracle's own snapshot time as "now" — eliminates state-lag skew
    # for IN_PROGRESS checks (elapsed is measured from this timestamp).
    state_at = datetime.fromisoformat(
        state["generatedAt"].replace("Z", "+00:00")
    )

    # Only stage=ci PRs exercise CI-progress ETA (our scope).
    # queue → queue.ts simulation; qa-deploy → deployment tracking.
    targets = [
        (r["repo"], p["number"],
         (p.get("stage") or {}).get("etaSeconds"),
         p.get("checks", []))
        for r in state["repos"]
        for p in r["prs"]
        if (p.get("stage") or {}).get("etaSeconds") is not None
        and (p.get("stage") or {}).get("stage") == "ci"
    ]

    if not targets:
        pytest.skip(
            "oracle has no stage=ci PRs with ETAs right now "
            "(all in queue/qa-deploy or no open PRs with CI running)"
        )

    mismatches = []
    for repo, num, oracle_eta, oracle_checks_raw in targets:
        # Convert oracle checks to our format, using state_at for elapsed → started_at
        checks = [_oracle_check_to_ours(c, state_at) for c in oracle_checks_raw]
        ours, _pct, _ov = compute_pr_eta(store, repo, checks, state_at)
        tol = max(2, 0.05 * oracle_eta)
        if ours is None or abs(ours - oracle_eta) > tol:
            mismatches.append((repo, num, oracle_eta, ours))

    assert not mismatches, (
        f"ETA parity mismatches (stage=ci only, oracle checks as input): {mismatches}"
    )
