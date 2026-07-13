"""Tests for backlog_from_prs — the per-repo qa/prod/admin backlog summary
derived from GitHub-Deployments ``deployed_envs`` (no deploy-config required).

Each merged PR's ``deployed_envs`` already encodes date-based liveness: an env
appears iff its newest successful deploy is at-or-after the PR's merge. So a PR
is "awaiting <category>" exactly when no env of that category is in its
deployed_envs — and merges older than the latest deploy are counted as done
(supersession falls out of the date compare, same as the corrected tile).
"""
from server.github.pr_status import PullRequestInfo
from server.pipeline.deploy_status import backlog_from_prs


def _pr(number: int, deployed_envs: tuple[str, ...]) -> PullRequestInfo:
    return PullRequestInfo(number=number, url=f"u/{number}", state="merged",
                           checks="passing", title="t", deployed_envs=deployed_envs)


def _open_pr(number: int) -> PullRequestInfo:
    return PullRequestInfo(number=number, url=f"u/{number}", state="open",
                           checks="passing", title="t")


def test_none_when_no_classifiable_deploy_envs():
    # reasonbridge: no deployments at all → no backlog line.
    assert backlog_from_prs([_pr(1, ()), _pr(2, ())]) is None


def test_open_prs_ignored():
    assert backlog_from_prs([_open_pr(1), _open_pr(2)]) is None


def test_kindash_three_way_breakout():
    prs = [
        # live everywhere (merged before every env's latest deploy) — awaiting nothing
        _pr(1, ("kindash / qa", "kindash / production",
                "kindash / admin-qa", "kindash / admin-production")),
        # live in qa + admin-qa only → awaiting prod (main) and NOT admin (admin-qa counts)
        _pr(2, ("kindash / qa", "kindash / admin-qa")),
        # merged after every deploy → awaiting qa, prod, and admin
        _pr(3, ()),
    ]
    # present categories: qa, prod, admin (each has ≥1 live PR via #1)
    assert backlog_from_prs(prs) == {
        "awaitingQa": 1,     # only #3
        "awaitingProd": 2,   # #2 and #3
        "awaitingAdmin": 1,  # only #3 (#2 is live in admin-qa)
    }


def test_admin_lumps_all_admin_envs():
    # A PR live in ANY admin env is not awaiting admin (user's "any admin env" rule).
    prs = [
        _pr(1, ("kindash / admin-production",)),  # establishes admin as a present category
        _pr(2, ("kindash / admin",)),             # live in bare admin → not awaiting admin
    ]
    out = backlog_from_prs(prs)
    assert out["awaitingAdmin"] == 0


def test_prod_only_repo_reports_only_prod():
    # steiner385/ccswitch: env is exactly "production" → prod bucket only.
    prs = [_pr(1, ("production",)), _pr(2, ())]
    assert backlog_from_prs(prs) == {"awaitingQa": 0, "awaitingProd": 1, "awaitingAdmin": 0}
