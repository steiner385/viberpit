from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import build_consolidation_prompt


def _pr(n):
    return PullRequestInfo(number=n, url=f"https://x/pull/{n}", state="open",
                           checks="passing", title=f"fix(deploy): item {n}")


def test_consolidation_prompt_names_every_pr_and_closes_and_supersedes():
    prompt = build_consolidation_prompt("o/r", "master", [_pr(11), _pr(12), _pr(13)])
    for n in (11, 12, 13):
        assert f"#{n}" in prompt
        assert f"Closes #{n}" in prompt
    assert "origin/master" in prompt          # branches off the base
    assert "Superseded by" in prompt          # closes originals with a linking comment
    assert "o/r" in prompt
