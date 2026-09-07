"""GitHub write endpoints (SPEC v2 Pack G, Task 7.6): POST a PR comment and file an
issue, both through the `server/github/ci_issues.py` helpers.

Two things are under test that no other suite covers:

* the **argv** each helper hands `gh` — these are the only outward-facing writes the
  assistant can make, so a wrong flag posts to the wrong place, not merely fails; and
* the **`via`-conditional authorship trailer**. One endpoint serves both the browser UI
  and the assistant token, and only the latter gets signed. The assistant half is driven
  through a real `ccas1:` bearer from a loopback client (the same way
  test_assistant_token.py does) rather than by poking `request.state`, because the
  middleware is what sets `via` and that is exactly the wiring being asserted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccswitch import brand  # noqa: E402
from server.app import create_app  # noqa: E402
from server.github import ci_issues  # noqa: E402
from server.sessions.orchestrator import SessionOrchestrator  # noqa: E402
from server.tests.fakes.fake_factory import fake_sdk_factory  # noqa: E402

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


def _gh_ok(payload: dict, sink: list[list[str]]):
    """A fake `run` that records argv and answers with `payload` as gh's JSON stdout."""
    def run(args, token=None, **kw):
        sink.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
    return run


def _gh_fail(sink: list[list[str]]):
    def run(args, token=None, **kw):
        sink.append(list(args))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
    return run


# --------------------------------------------------------------------------- #
# ci_issues helpers                                                            #
# --------------------------------------------------------------------------- #

def test_comment_on_pr_argv_and_url():
    calls: list[list[str]] = []
    url = ci_issues.comment_on_pr(
        "acme/billing", 42, "looks good", run=_gh_ok({"html_url": "https://c/1"}, calls))
    assert url == "https://c/1"
    assert calls == [["api", "repos/acme/billing/issues/42/comments", "-f", "body=looks good"]]


def test_comment_on_pr_returns_none_when_gh_fails():
    calls: list[list[str]] = []
    assert ci_issues.comment_on_pr("acme/billing", 42, "x", run=_gh_fail(calls)) is None


def test_create_issue_keeps_the_ci_doctor_label_by_default():
    """`labels=None` must stay the ci-doctor default — the CI/CD Doctor loop is the
    existing caller and its issues are identified by that label."""
    calls: list[list[str]] = []
    number = ci_issues.create_issue(
        "acme/billing", "t", "b", run=_gh_ok({"number": 7}, calls))
    assert number == 7
    create = calls[-1]
    assert f"labels[]={ci_issues.LABEL}" in create
    assert calls[0][:2] == ["api", "repos/acme/billing/labels"]


def test_create_issue_applies_explicit_labels_and_none_at_all():
    """`[]` means "no labels" — distinct from `None`, which means "the ci-doctor one"."""
    calls: list[list[str]] = []
    ci_issues.create_issue("acme/billing", "t", "b", labels=["bug", "p1"],
                           run=_gh_ok({"number": 9}, calls))
    create = calls[-1]
    assert "labels[]=bug" in create and "labels[]=p1" in create
    assert f"labels[]={ci_issues.LABEL}" not in create

    calls.clear()
    ci_issues.create_issue("acme/billing", "t", "b", labels=[],
                           run=_gh_ok({"number": 10}, calls))
    assert calls == [["api", "repos/acme/billing/issues", "-f", "title=t", "-f", "body=b"]]


def test_with_authorship_only_signs_assistant_writes():
    trailer = f"— posted by the {brand.NAME} assistant on behalf of me@example.com"
    signed = ci_issues.with_authorship("hi", via="assistant", identity="me@example.com")
    assert signed == f"hi\n\n{trailer}"
    for via in (None, "cli", "mcp-external"):
        assert ci_issues.with_authorship("hi", via=via, identity="me@example.com") == "hi"


def test_issue_url_is_the_canonical_html_url():
    assert ci_issues.issue_url("acme/billing", 12) == "https://github.com/acme/billing/issues/12"


@pytest.mark.parametrize("repo,ok", [
    ("acme/billing", True), ("a.c-me_1/bil.ling-2", True),
    ("acme/foo..bar", True),          # a legal repo name that merely contains dots
    ("acme/..", False), ("../acme", False), ("acme/.", False),
    ("acme", False), ("acme/billing/x", False), ("", False), ("acme/", False),
    ("acme/bil ling", False), ("acme/bil?ling", False),
])
def test_valid_repo_guards_the_gh_path_segment(repo, ok):
    assert ci_issues.valid_repo(repo) is ok


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #

def _make_client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def _assistant_headers(identity: str = "asst@example.com") -> dict[str, str]:
    from server.assistant.token import mint_token
    return {"Authorization": "Bearer " + mint_token(identity, "asst-1")}


def _fake_comment(sink: list[tuple], result="https://c/1"):
    def fake(repo, number, body, **kw):
        sink.append((repo, number, body))
        return result
    return fake


def _fake_create(sink: list[tuple], result=21):
    def fake(repo, title, body, *, labels=None, **kw):
        sink.append((repo, title, body, labels))
        return result
    return fake


# The endpoints reach gh through the helpers above, whose argv is already pinned by the
# unit tests; here the helper itself is the seam, so these assert what the ROUTE decides —
# the trailer, the label default, the status codes.

def test_pr_comment_requires_identity():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/prs/acme/billing/42/comments", json={"body": "hi"})
    assert r.status_code == 401


def test_pr_comment_from_the_ui_has_no_trailer():
    client = _make_client()
    seen: list[tuple] = []
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "comment_on_pr", _fake_comment(seen)):
        r = client.post("/api/prs/acme/billing/42/comments",
                        json={"body": "looks good"}, headers=_H)
    assert r.status_code == 201, r.text
    assert r.json() == {"url": "https://c/1"}
    assert seen == [("acme/billing", 42, "looks good")]


def test_pr_comment_from_the_assistant_is_signed():
    client = _make_client()
    seen: list[tuple] = []
    loopback = TestClient(client.app, client=("127.0.0.1", 5000), raise_server_exceptions=False)
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "comment_on_pr", _fake_comment(seen)):
        r = loopback.post("/api/prs/acme/billing/42/comments",
                          json={"body": "looks good"},
                          headers=_assistant_headers("owner@example.com"))
    assert r.status_code == 201, r.text
    body = seen[0][2]
    assert body == ("looks good\n\n— posted by the "
                    f"{brand.NAME} assistant on behalf of owner@example.com")


def test_pr_comment_rejects_an_oversized_body():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/prs/acme/billing/42/comments",
                        json={"body": "x" * (ci_issues.BODY_MAX_CHARS + 1)}, headers=_H)
    assert r.status_code == 422


def test_pr_comment_rejects_a_traversal_repo_segment():
    """`..` in a path segment would retarget the write; the route must not reach gh."""
    client = _make_client()
    seen: list[tuple] = []
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "comment_on_pr", _fake_comment(seen)):
        r = client.post("/api/prs/acme/../42/comments", json={"body": "hi"}, headers=_H)
    assert r.status_code in (404, 422), r.status_code
    assert seen == []


def test_pr_comment_502s_when_github_write_fails():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "comment_on_pr", _fake_comment([], result=None)):
        r = client.post("/api/prs/acme/billing/42/comments", json={"body": "hi"}, headers=_H)
    assert r.status_code == 502


def test_create_issue_endpoint_returns_number_and_url():
    client = _make_client()
    seen: list[tuple] = []
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "create_issue", _fake_create(seen)):
        r = client.post("/api/github/issues",
                        json={"repo": "acme/billing", "title": "Flaky checkout test",
                              "body": "It fails ~1 in 5 runs.", "labels": ["bug"]},
                        headers=_H)
    assert r.status_code == 201, r.text
    assert r.json() == {"number": 21, "url": "https://github.com/acme/billing/issues/21"}
    assert seen == [("acme/billing", "Flaky checkout test", "It fails ~1 in 5 runs.", ["bug"])]


def test_create_issue_endpoint_applies_no_label_when_none_given():
    """An issue filed here must not inherit the CI/CD Doctor's own label: `[]`, not `None`."""
    client = _make_client()
    seen: list[tuple] = []
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "create_issue", _fake_create(seen)):
        r = client.post("/api/github/issues",
                        json={"repo": "acme/billing", "title": "t", "body": "b"}, headers=_H)
    assert r.status_code == 201, r.text
    assert seen[0][3] == []


def test_create_issue_from_the_assistant_is_signed():
    client = _make_client()
    seen: list[tuple] = []
    loopback = TestClient(client.app, client=("127.0.0.1", 5000), raise_server_exceptions=False)
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "create_issue", _fake_create(seen)):
        r = loopback.post("/api/github/issues",
                          json={"repo": "acme/billing", "title": "t", "body": "b"},
                          headers=_assistant_headers("owner@example.com"))
    assert r.status_code == 201, r.text
    assert seen[0][2].endswith("assistant on behalf of owner@example.com")


@pytest.mark.parametrize("repo", ["acme", "acme/billing/extra", "", "/billing", "acme/",
                                  "acme/..", "acme/bil ling"])
def test_create_issue_rejects_a_malformed_repo(repo):
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/github/issues",
                        json={"repo": repo, "title": "t", "body": "b"}, headers=_H)
    assert r.status_code == 422


def test_create_issue_rejects_an_oversized_body():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/github/issues",
                        json={"repo": "acme/billing", "title": "t",
                              "body": "x" * (ci_issues.BODY_MAX_CHARS + 1)}, headers=_H)
    assert r.status_code == 422


def test_create_issue_502s_when_github_write_fails():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client, \
            patch.object(ci_issues, "create_issue", _fake_create([], result=None)):
        r = client.post("/api/github/issues",
                        json={"repo": "acme/billing", "title": "t", "body": "b"}, headers=_H)
    assert r.status_code == 502
