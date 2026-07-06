"""Unit tests for gitrepos.list_accessible_repos GraphQL discovery.

Regression guard for the org-repo gap: repos the user can reach only via org
membership / base-permission / implicit org-owner admin (i.e. WITHOUT an
explicit collaborator or team grant) never appear in ``viewer.repositories``
under any affiliation, even though ``repository(owner,name)`` returns ADMIN.
Discovery must union ``viewer.repositories`` with ``organization.repositories``
for every org in ``viewer.organizations``, deduped by ``nameWithOwner``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccswitch import gitrepos


def _ok(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=0, stdout=json.dumps(payload), stderr=""
    )


def _repo_node(name_with_owner: str, *, archived: bool = False) -> dict:
    owner, _, name = name_with_owner.partition("/")
    return {
        "nameWithOwner": name_with_owner,
        "name": name,
        "owner": {"login": owner},
        "isPrivate": True,
        "isArchived": archived,
        "url": f"https://github.com/{name_with_owner}",
        "pushedAt": "2026-01-01T00:00:00Z",
        "defaultBranchRef": {"name": "main"},
    }


def _query_of(args: list[str]) -> str:
    """Extract the GraphQL query string from a gh argv."""
    for a in args:
        if a.startswith("query="):
            return a[len("query=") :]
    return ""


def _fake_gh(viewer_repos, orgs, org_repos):
    """Route mocked gh graphql calls by which query they carry."""

    def _side_effect(args, token=None):
        q = _query_of(args)
        if "organizations" in q:
            return _ok({"data": {"viewer": {"organizations": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"login": o} for o in orgs],
            }}}})
        if "organization(" in q:
            # single-org enumeration; org login is passed via -F org=...
            org = next((a.split("=", 1)[1] for a in args if a.startswith("org=")), None)
            nodes = org_repos.get(org, [])
            return _ok({"data": {"organization": {"repositories": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }}}})
        # default: viewer.repositories
        return _ok({"data": {"viewer": {"repositories": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": viewer_repos,
        }}}})

    return _side_effect


def test_includes_org_repo_without_explicit_grant():
    """cairnea/infra (org-owner admin only) must appear via org enumeration."""
    viewer_repos = [_repo_node("cairnea/KinDash")]  # explicit collaborator only
    orgs = ["cairnea"]
    org_repos = {"cairnea": [
        _repo_node("cairnea/KinDash"),
        _repo_node("cairnea/infra"),
    ]}
    with patch.object(gitrepos, "_run_gh", side_effect=_fake_gh(viewer_repos, orgs, org_repos)):
        repos = gitrepos.list_accessible_repos()

    names = [r["nameWithOwner"] for r in repos]
    assert "cairnea/infra" in names, f"org repo missing from discovery: {names}"


def test_dedupes_repo_present_in_both_sets():
    """A repo in both viewer.repositories and an org must appear exactly once."""
    viewer_repos = [_repo_node("cairnea/KinDash")]
    orgs = ["cairnea"]
    org_repos = {"cairnea": [_repo_node("cairnea/KinDash"), _repo_node("cairnea/infra")]}
    with patch.object(gitrepos, "_run_gh", side_effect=_fake_gh(viewer_repos, orgs, org_repos)):
        repos = gitrepos.list_accessible_repos()

    names = [r["nameWithOwner"] for r in repos]
    assert names.count("cairnea/KinDash") == 1, f"duplicate KinDash: {names}"


def test_archived_org_repos_excluded():
    """Archived org repos stay out of the chooser, same as archived viewer repos."""
    viewer_repos = []
    orgs = ["cairnea"]
    org_repos = {"cairnea": [_repo_node("cairnea/old", archived=True)]}
    with patch.object(gitrepos, "_run_gh", side_effect=_fake_gh(viewer_repos, orgs, org_repos)):
        repos = gitrepos.list_accessible_repos()

    names = [r["nameWithOwner"] for r in repos]
    assert "cairnea/old" not in names, f"archived org repo leaked: {names}"
