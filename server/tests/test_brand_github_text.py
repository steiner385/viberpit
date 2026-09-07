"""Everything the automations post to GitHub names the product via brand.NAME.
These strings are public (issue comments, label descriptions), so the flip is
asserted here rather than left to a grep."""
from types import SimpleNamespace

from ccswitch import brand
from server.automations import backlog_github
from server.automations.cicd import findings
from server.github import ci_issues


def _capture():
    calls: list[list[str]] = []

    def run(args, token=None):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="")
    return calls, run


def _bodies(calls):
    return [args[args.index("--body") + 1] for args in calls if "--body" in args]


def test_escalation_comment_names_the_product(monkeypatch):
    monkeypatch.setattr(backlog_github.gitrepos, "app_token_for_repo", lambda repo: None)
    calls, run = _capture()
    backlog_github._escalate_issue("o/r#1", "docs-only diff", "https://pr/1", gh_run=run)
    assert any(f"by {brand.NAME} Backlog Runner" in b for b in _bodies(calls))
    assert not any("ccswitch" in b for b in _bodies(calls))


def test_release_comment_names_the_product(monkeypatch):
    monkeypatch.setattr(backlog_github.gitrepos, "app_token_for_repo", lambda repo: None)
    calls, run = _capture()
    backlog_github._release_tracking_issue("o/r#2", 3, "https://pr/2", gh_run=run)
    assert any(f"{brand.NAME} Backlog Runner left it open" in b for b in _bodies(calls))
    assert not any("ccswitch" in b for b in _bodies(calls))


def test_label_descriptions_name_the_product(monkeypatch):
    monkeypatch.setattr(backlog_github.gitrepos, "app_token_for_repo", lambda repo: None)
    calls, run = _capture()
    backlog_github.ensure_claim_labels(["o/r"], gh_run=run)
    descs = [args[args.index("--description") + 1] for args in calls]
    assert descs and all(d == f"{brand.SLUG} backlog-runner" for d in descs)

    calls2, run2 = _capture()
    ci_issues._ensure_label("o/r", ci_issues.LABEL, run2)
    assert any(f"description=Filed by the {brand.NAME} CI/CD Doctor" in a
               for args in calls2 for a in args)

    # Any other label is created by the write endpoints on behalf of a human or the
    # assistant, so its description names the product without claiming the doctor filed it.
    calls3, run3 = _capture()
    ci_issues._ensure_label("o/r", "needs-triage", run3)
    assert any(f"description=Applied by {brand.NAME}" in a
               for args in calls3 for a in args)
    assert not any("ccswitch" in a for args in calls3 for a in args)


def test_cicd_issue_body_names_the_product():
    body = findings.issue_body("flaky", {"x": 1})
    assert body.endswith(f"Filed by the {brand.NAME} CI/CD Doctor (propose mode).")
    assert "ccswitch" not in body
