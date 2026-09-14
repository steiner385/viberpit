"""Immutable target identities must never degrade to alias-based exclusion."""
from unittest.mock import AsyncMock

import pytest

from server.automations.ownership import CanonicalTarget
from server.automations.target_identity import TargetIdentityError, resolve_targets


def repository(number=1, node_id="I_one", kind="Issue", repo="owner/current"):
    path = "issues" if kind == "Issue" else "pull"
    return {"nameWithOwner": repo, "item": {
        "__typename": kind, "id": node_id, "number": number,
        "url": f"https://github.com/{repo}/{path}/{number}",
        "repository": {"nameWithOwner": repo},
    }}


@pytest.mark.asyncio
async def test_aliases_case_renames_and_overlapping_batches_share_keys():
    fetch = AsyncMock(side_effect=[
        {"data": {"r0": repository(), "r1": repository(),
                  "r2": repository(2, "I_two")}},
        {"data": {"r0": repository(), "r1": repository(3, "I_three")}},
    ])
    left = await resolve_targets(
        ["owner/old#1", "OWNER/CURRENT#1", "owner/current#2", "owner/old#1"],
        kind="issue", fetch=fetch)
    right = await resolve_targets(["owner/current#1", "owner/current#3"],
                                  kind="issue", fetch=fetch)
    assert left == (CanonicalTarget("I_one", "owner/current#1"),
                    CanonicalTarget("I_two", "owner/current#2"))
    assert {t.key for t in left} & {t.key for t in right} == {"github:I_one"}
    assert fetch.await_count == 2
    assert fetch.await_args_list[0].args[0] == "owner"
    assert "r3:" not in fetch.await_args_list[0].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["x", "a/b#0", "a/b#-1", "a/b#١", "a/b#1;touch /tmp/x",
                                   'a/b\" ){viewer{login}}#1', "../b#1", "a/b/c#1",
                                   "https://evil.test/a/b/issues/1", "a/b#1#2"])
async def test_malformed_refs_fail_before_transport(ref):
    fetch = AsyncMock()
    with pytest.raises(TargetIdentityError):
        await resolve_targets(["a/b#1", ref], kind="issue", fetch=fetch)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, {"errors": [{"message": "secret"}]},
    {"data": None}, {"data": {"r0": None}}, {"data": {"r0": {"item": None}}}])
async def test_missing_identity_fails_closed(payload):
    with pytest.raises(TargetIdentityError):
        await resolve_targets(["owner/current#1"], kind="issue",
                              fetch=AsyncMock(return_value=payload))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"id": None}, {"id": ""}, {"id": "bad id"}, {"__typename": "PullRequest"},
    {"__typename": "Repository"}, {"number": 2}, {"number": True},
    {"url": "https://enterprise.test/owner/current/issues/1"},
    {"url": "https://github.com.evil.test/owner/current/issues/1"},
    {"url": "https://github.com@evil.test/owner/current/issues/1"},
    {"url": "https://github.com/owner/other/issues/1"},
    {"repository": {"nameWithOwner": "owner/other"}},
])
async def test_unverified_or_wrong_kind_response_fails_closed(changes):
    repo = repository()
    repo["item"].update(changes)
    with pytest.raises(TargetIdentityError):
        await resolve_targets(["owner/current#1"], kind="issue",
                              fetch=AsyncMock(return_value={"data": {"r0": repo}}))


@pytest.mark.asyncio
async def test_unavailable_is_sanitized_and_cancellation_propagates():
    import asyncio
    with pytest.raises(TargetIdentityError, match="unavailable") as exc:
        await resolve_targets(["owner/current#1"], kind="issue",
                              fetch=AsyncMock(side_effect=RuntimeError("token-secret")))
    assert "token-secret" not in str(exc.value)
    with pytest.raises(asyncio.CancelledError):
        await resolve_targets(["owner/current#1"], kind="issue",
                              fetch=AsyncMock(side_effect=asyncio.CancelledError))


@pytest.mark.asyncio
async def test_pr_intent_and_empty_batch():
    fetch = AsyncMock(return_value={"data": {"r0": repository(kind="PullRequest", node_id="PR_one")}})
    assert await resolve_targets([], kind="pr", fetch=fetch) == ()
    fetch.assert_not_awaited()
    assert (await resolve_targets(["owner/current#1"], kind="pr", fetch=fetch))[0].key == "github:PR_one"
    assert "pullRequest(number: 1)" in fetch.await_args.args[1]
    with pytest.raises(TargetIdentityError):
        await resolve_targets(["owner/current#1"], kind="repository", fetch=fetch)


@pytest.mark.asyncio
async def test_default_transport_pins_authority_and_reuses_owner_credentials(monkeypatch):
    import json
    import subprocess
    from unittest.mock import Mock

    from ccswitch import gitrepos

    token = Mock(return_value="installation-token")
    run = Mock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps({"data": {"r0": repository()}}), ""))
    monkeypatch.setenv("GH_HOST", "enterprise.test")
    monkeypatch.setattr(gitrepos, "app_token_for_owner", token)
    monkeypatch.setattr(gitrepos, "_run_gh", run)
    assert await resolve_targets(["owner/current#1"], kind="issue") == (
        CanonicalTarget("I_one", "owner/current#1"),)
    token.assert_called_once_with("owner")
    assert run.call_args.args[0][:4] == ["api", "--hostname", "github.com", "graphql"]
    assert run.call_args.kwargs == {"token": "installation-token", "timeout": 30}


@pytest.mark.asyncio
async def test_multiple_owners_and_bounded_chunks_resolve_entire_batch():
    fetch = AsyncMock(side_effect=[
        {"data": {f"r{i}": repository(i + 1, f"I_{i + 1}") for i in range(50)}},
        {"data": {"r0": repository(51, "I_51")}},
        {"data": {"r0": repository(1, "I_other", repo="other/repo")}},
    ])
    result = await resolve_targets(
        [f"owner/current#{i}" for i in range(1, 52)] + ["other/repo#1"],
        kind="issue", fetch=fetch)
    assert len(result) == 52
    assert [call.args[0] for call in fetch.await_args_list] == ["owner", "owner", "other"]
    assert result[-1] == CanonicalTarget("I_other", "other/repo#1")


@pytest.mark.asyncio
async def test_incomplete_second_owner_never_returns_partial_targets():
    fetch = AsyncMock(side_effect=[
        {"data": {"r0": repository()}}, {"data": {"r0": None}},
    ])
    with pytest.raises(TargetIdentityError):
        await resolve_targets(["owner/current#1", "other/repo#2"], kind="issue", fetch=fetch)
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_conflicting_display_identity_for_same_node_fails_closed():
    fetch = AsyncMock(return_value={"data": {
        "r0": repository(), "r1": repository(number=2),
    }})
    with pytest.raises(TargetIdentityError, match="inconsistent"):
        await resolve_targets(["owner/current#1", "owner/current#2"], kind="issue", fetch=fetch)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["owner/repo#2147483648", "owner/repo#²", "owner/.#1", None])
async def test_additional_invalid_refs_never_reach_transport(ref):
    fetch = AsyncMock()
    with pytest.raises(TargetIdentityError):
        await resolve_targets([ref], kind="issue", fetch=fetch)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_resolution_includes_cross_repo_closing_issue_and_refuses_truncation():
    pr = repository(2, "PR_two", "PullRequest")
    pr["item"]["closingIssuesReferences"] = {
        "pageInfo": {"hasNextPage": False},
        "nodes": [repository(7, "I_seven", repo="other/issues")["item"]],
    }
    fetch = AsyncMock(return_value={"data": {"r0": pr}})
    result = await resolve_targets(["owner/old#2"], kind="pr", fetch=fetch,
                                   include_linked_issues=True)
    assert {t.key for t in result} == {"github:PR_two", "github:I_seven"}
    assert "closingIssuesReferences(first: 100)" in fetch.await_args.args[1]
    pr["item"]["closingIssuesReferences"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(TargetIdentityError, match="complete PR linkage"):
        await resolve_targets(["owner/current#2"], kind="pr", fetch=fetch,
                              include_linked_issues=True)


@pytest.mark.asyncio
async def test_repository_snapshot_deduplicates_case_and_preserves_alias_evidence():
    from server.automations.target_identity import resolve_repositories
    fetch = AsyncMock(return_value={"data": {
        "r0": {"nameWithOwner": "Owner/current"},
        "r1": {"nameWithOwner": "Owner/current"},
    }})
    result = await resolve_repositories(
        ["owner/old", "OWNER/OLD", "owner/current", "invalid"], fetch=fetch)
    assert result == {"owner/old": "Owner/current", "OWNER/OLD": "Owner/current",
                      "owner/current": "Owner/current", "invalid": None}
    assert fetch.await_count == 1
    assert fetch.await_args.args[0] == "owner"
    assert "r2:" not in fetch.await_args.args[1]


@pytest.mark.asyncio
async def test_repository_snapshot_partial_or_failed_chunks_remain_unknown():
    from server.automations.target_identity import resolve_repositories
    fetch = AsyncMock(side_effect=[
        {"data": {"r0": {"nameWithOwner": "a/good"},
                  "r1": {"nameWithOwner": "bad/path/extra"}}},
        RuntimeError("secret"),
        {"data": {"r0": {"nameWithOwner": "c/good"}}, "errors": [{"message": "secret"}]},
    ])
    assert await resolve_repositories(
        ["a/good", "a/bad", "a/missing", "b/private", "c/good"], fetch=fetch
    ) == {"a/good": "a/good", "a/bad": None, "a/missing": None,
          "b/private": None, "c/good": None}


@pytest.mark.asyncio
async def test_repository_snapshot_bounds_chunks_and_propagates_cancellation():
    import asyncio
    from server.automations.target_identity import resolve_repositories
    fetch = AsyncMock(return_value={"data": {}})
    result = await resolve_repositories([f"a/r{i}" for i in range(101)], fetch=fetch)
    assert len(result) == 101 and set(result.values()) == {None}
    assert fetch.await_count == 3
    assert all("r50:" not in call.args[1] for call in fetch.await_args_list)
    with pytest.raises(asyncio.CancelledError):
        await resolve_repositories(["a/b"], fetch=AsyncMock(side_effect=asyncio.CancelledError))
