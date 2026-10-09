"""Author trust for the dispatch paths that do NOT re-rank the queue.

``queue.skip_reason`` gates the ranked pool, but three paths dispatch a ref they were
handed: the manual/assistant ``POST /backlog-runner/dispatch``, a pending (already
claimed) batch retried through the propose-tap, and the approve-escalation tap. A
public repo lets anyone open an issue, so each must judge the MIRRORED author against
the owning product's ``trusted_authors`` itself. Fail-closed: an unparsable ref, a ref
with no mirrored issue, or an issue with no author is untrusted.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.automations.product_policy import policy_for_repo, trusted_authors
from server.db.models import WorkItem
from server.pm.queue import is_trusted_author
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
