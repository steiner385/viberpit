"""GhWrite: the map-changes GitHub seam (roadmap spec §9), against a recorded
fake `gh` — the "fake GitHub layer" §14 asks for. Every method is argv-checked
so a future change to a REST path or the GraphQL document is a test diff."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

from server.pm.map_changes_gh import GhWrite, GhWriteError

REPO = "cairnea/KinDash"
PAT_ENV = {"VIBERPIT_GH_PROJECT_TOKEN": "pat-tok"}


class FakeGh:
    """Answers `gh` calls from a queue of (stdout, returncode, stderr) and records argv + stdin + token."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def __call__(self, args, token=None, *, input=None, timeout=None):
        self.calls.append((list(args), token, input))
        out, rc, err = self.answers.pop(0)
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=err)


def _gh(answers, *, app_token="app-tok", env=None):
    fake = FakeGh(answers)
    gh = GhWrite(REPO, run=fake, app_token=lambda repo: app_token,
                 env=env if env is not None else PAT_ENV)
    return gh, fake


def test_default_branch_head_and_ensure_branch_creates_from_it():
    gh, fake = _gh([
        (json.dumps({"object": {"sha": "abc"}}), 0, ""),               # GET git/ref/heads/master
        ("", 1, "HTTP 404: Not Found"),                                 # GET git/ref/heads/pm/map-changes-kindash
        (json.dumps({"ref": "refs/heads/pm/map-changes-kindash", "object": {"sha": "abc"}}), 0, ""),  # POST git/refs
    ])
    assert gh.default_branch_head("master") == "abc"
    assert gh.ensure_branch("pm/map-changes-kindash", "abc") == "abc"
    assert fake.calls[0][0] == ["api", f"repos/{REPO}/git/ref/heads/master"]
    assert fake.calls[2][0][:4] == ["api", "-X", "POST", f"repos/{REPO}/git/refs"]
    assert "ref=refs/heads/pm/map-changes-kindash" in fake.calls[2][0]
    assert "sha=abc" in fake.calls[2][0]
    assert all(call[1] == "app-tok" for call in fake.calls)


def test_ensure_branch_returns_the_existing_head():
    gh, fake = _gh([(json.dumps({"object": {"sha": "def"}}), 0, "")])
    assert gh.ensure_branch("pm/map-changes-kindash", "abc") == "def"
    assert len(fake.calls) == 1


def test_branch_exists_is_a_probe_that_never_creates_the_ref():
    gh, fake = _gh([
        (json.dumps({"object": {"sha": "def"}}), 0, ""),
        ("", 1, "HTTP 404: Not Found"),
    ])
    assert gh.branch_exists("pm/map-changes-kindash") is True
    assert gh.branch_exists("pm/map-changes-kindash") is False
    assert fake.calls[0][0] == ["api", f"repos/{REPO}/git/ref/heads/pm/map-changes-kindash"]
    # No POST git/refs — unlike ensure_branch, this one only looks.
    assert all("-X" not in call[0] for call in fake.calls)


def test_find_open_pr_returns_the_head_branch_pr_or_none():
    # The LIST endpoint's shape: no `mergeable_state`, no `merged` — GitHub only
    # computes those for a single-PR read, which is why the caller re-reads.
    pr = {"number": 12, "html_url": "https://github.com/cairnea/KinDash/pull/12",
          "state": "open", "merged_at": None, "head": {"sha": "new1"}}
    gh, fake = _gh([(json.dumps([pr]), 0, ""), ("[]", 0, "")])
    found = gh.find_open_pr("pm/map-changes-kindash", "master")
    assert found is not None and found.number == 12 and found.head_sha == "new1"
    assert found.mergeable_state is None
    # `head` is qualified with the repo owner — GitHub ignores an unqualified one;
    # `base` keeps a stacked PR off the same branch from being adopted.
    assert fake.calls[0][0] == [
        "api",
        f"repos/{REPO}/pulls?head=cairnea:pm/map-changes-kindash&base=master&state=open"]
    assert gh.find_open_pr("pm/map-changes-kindash", "master") is None


def test_read_file_decodes_and_404_is_none():
    text = "---\nslug: x\n---\n# X\n"
    gh, fake = _gh([
        (json.dumps({"content": base64.b64encode(text.encode()).decode(), "sha": "blob1"}), 0, ""),
        ("", 1, "HTTP 404: Not Found"),
    ])
    assert gh.read_file("docs/product/features/x.md", "pm/map-changes-kindash") == (text, "blob1")
    assert gh.read_file("docs/product/features/y.md", "pm/map-changes-kindash") == (None, None)
    assert fake.calls[0][0] == ["api", f"repos/{REPO}/contents/docs/product/features/x.md?ref=pm/map-changes-kindash"]


def test_commit_file_uses_create_commit_on_branch_with_expected_head():
    gh, fake = _gh([(json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": "new1"}}}}), 0, "")])
    sha = gh.commit_file("pm/map-changes-kindash", "abc", "docs/product/features/x.md", "# X\n", "map(kindash): add capability x")
    assert sha == "new1"
    args, token, stdin = fake.calls[0]
    assert args == ["api", "graphql", "--input", "-"]
    payload = json.loads(stdin)
    assert "createCommitOnBranch" in payload["query"]
    v = payload["variables"]["input"]
    assert v["branch"] == {"repositoryNameWithOwner": REPO, "branchName": "pm/map-changes-kindash"}
    assert v["expectedHeadOid"] == "abc"
    assert v["message"] == {"headline": "map(kindash): add capability x"}
    assert v["fileChanges"]["additions"] == [{"path": "docs/product/features/x.md",
                                              "contents": base64.b64encode(b"# X\n").decode()}]


def test_commit_file_reports_a_nonexistent_expected_head_as_a_conflict_too():
    # Verified live: a sha that no longer exists answers type NOT_FOUND with this
    # wording; the caller's remedy is the same re-read of the head.
    gh, _ = _gh([(json.dumps({"errors": [{"type": "NOT_FOUND", "message": "No commit exists with specified expectedHeadOid 'abc'."}]}), 0, "")])
    with pytest.raises(GhWriteError) as e:
        gh.commit_file("b", "abc", "p", "t", "m")
    assert e.value.conflict is True


def test_commit_file_reports_a_moved_head_as_a_conflict():
    gh, _ = _gh([(json.dumps({"errors": [{"type": "STALE_DATA", "message": "Expected branch to point to \"abc\" but it did not.  Pull and try again."}]}), 0, "")])
    with pytest.raises(GhWriteError) as e:
        gh.commit_file("b", "abc", "p", "t", "m")
    assert e.value.step == "commit" and e.value.conflict is True


def test_open_read_close_pr_update_branch_and_delete_branch():
    pr = {"number": 12, "html_url": "https://github.com/cairnea/KinDash/pull/12", "state": "open",
          "merged": False, "merged_at": None, "head": {"sha": "new1"}, "mergeable_state": "behind"}
    gh, fake = _gh([
        (json.dumps(pr), 0, ""),                                   # POST pulls
        (json.dumps({**pr, "mergeable_state": "dirty"}), 0, ""),   # GET pulls/12
        ("", 0, ""),                                               # PATCH pulls/12 state=closed
        ("", 0, ""),                                               # PUT pulls/12/update-branch
        ("", 0, ""),                                               # DELETE git/refs/heads/...
    ])
    opened = gh.open_pr("pm/map-changes-kindash", "master", "Capability map changes: KinDash", "body")
    assert (opened.number, opened.url, opened.behind, opened.conflicting) == (12, pr["html_url"], True, False)
    argv = fake.calls[0][0]
    assert argv[:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]
    assert "draft=false" in argv or "-F" in argv
    # The title is a `-f` FIELD, not `-F` (which would read `@`-prefixed input
    # and coerce a numeric-looking title): pin the adjacency, not just presence.
    assert argv[argv.index("title=Capability map changes: KinDash") - 1] == "-f"
    assert argv[argv.index("head=pm/map-changes-kindash") - 1] == "-f"
    assert argv[argv.index("base=master") - 1] == "-f"
    read = gh.read_pr(12)
    assert read.conflicting is True and read.head_sha == "new1"
    gh.close_pr(12)
    assert fake.calls[2][0][:4] == ["api", "-X", "PATCH", f"repos/{REPO}/pulls/12"] and "state=closed" in fake.calls[2][0]
    gh.recovery_request(["-X", "PUT", f"repos/{REPO}/pulls/12/update-branch", "-f", "expected_head_sha=abc"])
    assert fake.calls[3][0][:6] == ["api", "--hostname", "github.com", "-X", "PUT", f"repos/{REPO}/pulls/12/update-branch"]
    gh.delete_branch("pm/map-changes-kindash")
    assert fake.calls[4][0][:4] == ["api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/pm/map-changes-kindash"]


def test_app_token_403_falls_back_to_the_pat_and_sticks():
    gh, fake = _gh([
        ("", 1, "HTTP 403: Resource not accessible by integration"),   # app token refused
        (json.dumps({"object": {"sha": "abc"}}), 0, ""),               # PAT works
        (json.dumps({"object": {"sha": "abc"}}), 0, ""),               # next call goes straight to the PAT
    ])
    assert gh.default_branch_head("master") == "abc"
    assert gh.default_branch_head("master") == "abc"
    assert [c[1] for c in fake.calls] == ["app-tok", "pat-tok", "pat-tok"]


def test_no_app_token_and_no_pat_means_ambient_gh():
    gh, fake = _gh([(json.dumps({"object": {"sha": "abc"}}), 0, "")], app_token=None, env={})
    gh.default_branch_head("master")
    assert fake.calls[0][1] is None


def test_other_failures_raise_with_the_step():
    gh, _ = _gh([("", 1, "HTTP 502: Bad Gateway")])
    with pytest.raises(GhWriteError) as e:
        gh.default_branch_head("master")
    assert e.value.step == "head" and "502" in e.value.detail and e.value.conflict is False
