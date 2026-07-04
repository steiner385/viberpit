import asyncio
from server.automations import archive_audit as aa


def _row(id, goal, repo="o/r", branch="feat/x", authored=("feat/x",)):
    return {"id": id, "repo": repo, "branch": branch,
            "authored_branches": list(authored), "goal": goal, "goal_source": "explicit"}


def _gh_returning(pr):
    def gh_fn(repo, branch):
        return [pr] if pr else []
    return gh_fn


def _pr(number=1, state="OPEN", is_draft=False, auto_merge=False):
    return {"number": number, "state": state, "isDraft": is_draft,
            "autoMergeRequest": {"enabledAt": "t"} if auto_merge else None,
            "url": f"http://x/{number}"}


def test_merged_pr_is_legit_not_unarchived():
    unarchived = []
    gh = _gh_returning(_pr(state="MERGED"))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append))
    assert out[0]["bucket"] == "legit" and unarchived == []


def test_unarmed_open_pr_is_premature_and_unarchived():
    unarchived = []
    gh = _gh_returning(_pr(state="OPEN", auto_merge=False))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append))
    assert out[0]["bucket"] == "premature" and unarchived == ["s1"]


def test_code_goal_no_pr_is_ambiguous_report_only():
    unarchived = []
    out = asyncio.run(aa.run_audit([_row("s1", "fix the cicd doctor")],
                                   _gh_returning(None), unarchived.append))
    assert out[0]["bucket"] == "ambiguous" and unarchived == []


def test_gh_error_is_ambiguous():
    def gh(repo, branch):
        raise RuntimeError("gh down")
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, lambda s: None))
    assert out[0]["bucket"] == "ambiguous"


def test_dry_run_does_not_unarchive():
    unarchived = []
    gh = _gh_returning(_pr(state="OPEN", auto_merge=False))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append, dry_run=True))
    assert out[0]["bucket"] == "premature" and unarchived == []
