"""Resolve mutable GitHub spellings before acquiring cross-automation ownership.

Only github.com is supported. Mirror IDs are not trusted without an authority
attestation; every requested item is resolved through GitHub before any claim.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from ccswitch import gitrepos

from server.automations.ownership import CanonicalTarget
from server.pm.gh_mutations import payload_from_gh
from server.pm.refs import split_ref


class TargetIdentityError(ValueError):
    """Resolution failed; callers must not fall back to display-ref ownership."""


async def _fetch(owner: str, query: str) -> dict:
    # Reuse the application's credential selection, subprocess isolation and
    # strict GraphQL decoder. Pin authority even when GH_HOST names Enterprise.
    token = await asyncio.to_thread(gitrepos.app_token_for_owner, owner)
    proc = await asyncio.to_thread(
        gitrepos._run_gh,
        ["api", "--hostname", "github.com", "graphql", "-f", f"query={query}"],
        token=token, timeout=30,
    )
    return payload_from_gh(proc, partial_ok=False)


def _repo(value: object) -> bool:
    return (isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value) is not None
            and value.partition("/")[2] not in {".", ".."})


async def resolve_targets(
    refs: Sequence[str], *, kind: Literal["issue", "pr"],
    fetch: Callable[[str, str], Awaitable[dict]] | None = None,
    include_linked_issues: bool = False,
) -> tuple[CanonicalTarget, ...]:
    """Resolve all targets, deduplicating aliases by immutable node ID.

    ``fetch`` is a test seam with the same github.com authority contract as the
    default transport. Queries are grouped by credential owner and bounded to
    50 aliases. Any malformed input, partial reply or failed chunk rejects the
    entire batch; no caller can acquire a partially resolved target set.
    """
    if include_linked_issues and kind != "pr":
        raise TargetIdentityError("linked issues require a PR")
    if kind not in {"issue", "pr"}:
        raise TargetIdentityError("unsupported target kind")
    groups: dict[str, list[tuple[str, int]]] = {}
    seen: set[tuple[str, int]] = set()
    for ref in refs:
        if not isinstance(ref, str):
            raise TargetIdentityError("invalid target reference")
        try:
            parsed = split_ref(ref)
        except ValueError:
            parsed = None
        if (parsed is None or not _repo(parsed[0]) or parsed[1] <= 0
                or parsed[1] > 2**31 - 1
                or not re.fullmatch(r"[0-9]+", ref.rpartition("#")[2])):
            raise TargetIdentityError("invalid target reference")
        repo, number = parsed
        key = (repo.lower(), number)
        if key not in seen:
            seen.add(key)
            groups.setdefault(repo.partition("/")[0].lower(), []).append((repo, number))
    field, typename, path = (("issue", "Issue", "issues") if kind == "issue"
                             else ("pullRequest", "PullRequest", "pull"))
    resolved: dict[str, CanonicalTarget] = {}
    for owner, items in groups.items():
        for start in range(0, len(items), 50):
            chunk = items[start:start + 50]
            aliases = []
            for index, (repo, number) in enumerate(chunk):
                login, _, name = repo.partition("/")
                aliases.append(
                    f"r{index}: repository(owner: {json.dumps(login)}, name: {json.dumps(name)}) {{"
                    f" nameWithOwner item: {field}(number: {number}) {{"
                    " __typename id number url repository { nameWithOwner } "
                    + ("closingIssuesReferences(first: 100) { pageInfo { hasNextPage } "
                       "nodes { __typename id number url repository { nameWithOwner } } } "
                       if include_linked_issues else "") + "} }")
            try:
                payload = await (fetch or _fetch)(owner, "query { " + " ".join(aliases) + " }")
            except Exception:
                raise TargetIdentityError("target identity unavailable") from None
            if not isinstance(payload, dict) or payload.get("errors"):
                raise TargetIdentityError("target identity unavailable")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise TargetIdentityError("target identity unavailable")
            for index, (_, number) in enumerate(chunk):
                repository = data.get(f"r{index}")
                if not isinstance(repository, dict):
                    raise TargetIdentityError("target identity unavailable")
                canonical_repo = repository.get("nameWithOwner")
                item = repository.get("item")
                if not _repo(canonical_repo) or not isinstance(item, dict):
                    raise TargetIdentityError("target identity unavailable")
                if (item.get("__typename") != typename
                        or type(item.get("number")) is not int or item["number"] != number
                        or item.get("repository") != {"nameWithOwner": canonical_repo}
                        or item.get("url") != f"https://github.com/{canonical_repo}/{path}/{number}"
                        or not isinstance(item.get("id"), str)):
                    raise TargetIdentityError("unverified target identity")
                try:
                    target = CanonicalTarget(item["id"], f"{canonical_repo}#{number}")
                except ValueError:
                    raise TargetIdentityError("unverified target identity") from None
                existing = resolved.get(target.node_id)
                if existing is not None and existing.display_ref.lower() != target.display_ref.lower():
                    raise TargetIdentityError("inconsistent target identity")
                resolved.setdefault(target.node_id, target)
                if include_linked_issues:
                    links = item.get("closingIssuesReferences")
                    if (not isinstance(links, dict)
                            or links.get("pageInfo") != {"hasNextPage": False}
                            or not isinstance(links.get("nodes"), list)):
                        raise TargetIdentityError("complete PR linkage unavailable")
                    for linked in links["nodes"]:
                        if not isinstance(linked, dict):
                            raise TargetIdentityError("unverified linked issue")
                        linked_repository = linked.get("repository")
                        linked_repo = (linked_repository.get("nameWithOwner")
                                       if isinstance(linked_repository, dict) else None)
                        linked_number = linked.get("number")
                        if (linked.get("__typename") != "Issue" or not _repo(linked_repo)
                                or type(linked_number) is not int or linked_number <= 0
                                or linked.get("url") !=
                                f"https://github.com/{linked_repo}/issues/{linked_number}"
                                or not isinstance(linked.get("id"), str)):
                            raise TargetIdentityError("unverified linked issue")
                        try:
                            issue = CanonicalTarget(linked["id"], f"{linked_repo}#{linked_number}")
                        except ValueError:
                            raise TargetIdentityError("unverified linked issue") from None
                        previous = resolved.get(issue.node_id)
                        if previous and previous.display_ref.lower() != issue.display_ref.lower():
                            raise TargetIdentityError("inconsistent target identity")
                        resolved.setdefault(issue.node_id, issue)
    return tuple(resolved.values())
