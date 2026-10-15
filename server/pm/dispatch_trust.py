"""Author trust for the dispatch paths that do NOT re-rank the queue.

``queue.skip_reason`` gates the ranked pool, but three paths dispatch a ref they were
handed: the manual/assistant ``POST /backlog-runner/dispatch``, a pending (already
claimed) batch retried through the propose-tap, and the approve-escalation tap. A
public repo lets anyone open an issue, so each must judge the MIRRORED author against
the owning product's ``trusted_authors`` itself. Fail-closed: an unparsable ref, a ref
with no mirrored issue, or an issue with no author is untrusted.

The operator's ``viberpit-ignore`` label is the other verdict those paths must
re-apply themselves (#3111): the ranked pool honours it for every product, but a
ref handed straight to a dispatch path never meets that skip (``ignored_refs``).

The same rule gates every other automation that puts a mirrored issue's or PR's text
in front of an agent (PM Triage classification, epic adoption's spec seed, PR Doctor):
``trusted_authors_by_repo`` + ``author_trusted`` judge already-loaded rows in bulk.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.automations.product_policy import load_policies, policy_for_repo, trusted_authors
from server.db.models import WorkItem
from server.pm.queue import IGNORE_LABEL, is_trusted_author
from server.pm.refs import split_ref


async def untrusted_refs(db: AsyncSession, refs: Iterable[str]) -> list[str]:
    """The refs among *refs* whose issue author is not trusted under its repo's
    product policy, in the order given."""
    policies: dict[str, frozenset[str]] = {}
    out: list[str] = []
    for ref in refs:
        parts = split_ref(ref)
        wi = None
        if parts is not None:
            repo, number = parts
            wi = (await db.execute(select(WorkItem).where(
                WorkItem.repo == repo, WorkItem.number == number,
                WorkItem.kind == "issue"))).scalar_one_or_none()
        if wi is None:
            out.append(ref)
            continue
        if repo not in policies:
            policies[repo] = trusted_authors(await policy_for_repo(db, repo))
        if not is_trusted_author(wi.author or "", policies[repo]):
            out.append(ref)
    return out


async def ignored_refs(db: AsyncSession, refs: Iterable[str]) -> list[str]:
    """The refs among *refs* whose mirrored issue carries ``IGNORE_LABEL`` (any
    case), in the order given. An unmirrored ref is not ignored here: it has no
    labels to read, and ``untrusted_refs`` already refuses it fail-closed."""
    out: list[str] = []
    for ref in refs:
        parts = split_ref(ref)
        if parts is None:
            continue
        repo, number = parts
        labels = (await db.execute(select(WorkItem.labels).where(
            WorkItem.repo == repo, WorkItem.number == number,
            WorkItem.kind == "issue"))).scalar_one_or_none()
        if any(str(label).strip().lower() == IGNORE_LABEL for label in labels or ()):
            out.append(ref)
    return out


async def trusted_authors_by_repo(db: AsyncSession,
                                  repos: Iterable[str | None]) -> dict[str, frozenset[str]]:
    """repo -> the ``trusted_authors`` of the ONE product that owns it (the same lookup
    as ``policy_for_repo``, batched). A repo no product uniquely owns is ABSENT, which
    ``author_trusted`` reads as "nobody is trusted" (fail closed)."""
    from server.pm.repo_project import owning_projects_by_repo
    wanted = {r for r in repos if r}
    if not wanted:
        return {}
    owners = await owning_projects_by_repo(db, wanted)
    if not owners:
        return {}
    policies = await load_policies(db)
    return {repo: trusted_authors(policies.get(owner.slug)) for repo, owner in owners.items()}


def author_trusted(by_repo: Mapping[str, frozenset[str]], repo: str | None,
                   author: str | None) -> bool:
    """Whether *author* is trusted for *repo* under ``trusted_authors_by_repo``'s map."""
    return is_trusted_author(author or "", by_repo.get(repo or "", frozenset()))


async def untrusted_prs(db: AsyncSession, repo: str, numbers: Iterable[int]) -> list[int]:
    """The PR numbers among *numbers* whose MIRRORED author is not trusted for *repo*
    (fail-closed: a PR the mirror does not hold is untrusted), in the order given."""
    nums = [int(n) for n in numbers]
    rows = (await db.execute(select(WorkItem.number, WorkItem.author).where(
        WorkItem.repo == repo, WorkItem.number.in_(nums), WorkItem.kind == "pr"))).all()
    authors = {n: a for n, a in rows}
    by_repo = await trusted_authors_by_repo(db, [repo])
    return [n for n in nums if n not in authors or not author_trusted(by_repo, repo, authors[n])]
