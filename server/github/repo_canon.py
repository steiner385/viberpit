"""Resolve a repo slug to GitHub's canonical ``nameWithOwner``.

GitHub forwards a renamed repository forever: ``repository(owner:, name:)`` for a
pre-rename slug still SUCCEEDS and answers with the canonical repository. Nothing
404s, so a stale alias never announces itself — it just silently becomes a second
identity for one repo everywhere a slug is used as a *string key*. That is not
hypothetical: this project's own pre-rename slug sat in ``enabled_repos``
alongside its own new name, and because pr-doctor's job key is
``"owner/repo#N"`` (:mod:`server.automations.pr_doctor_jobs`) the sweep spawned two
concurrent sessions on the same pull request, on the same branch.

This is the resolver for the *write* path — canonicalize before storing, so an
alias can never be persisted. It is the sibling of
:func:`server.pm.github_sync.canonical_repo_name`, which reads the canonical name
back out of a payload the PM sync loop already fetched for its own reasons; the
two answer the same question about different inputs (a bare slug here, a
repo-rooted query payload there).

Fails OPEN throughout. A config write must never be rejected, and a repo list must
never lose an entry, because GitHub was briefly unreachable — an alias is a
cosmetic and coordination problem, an emptied ``enabled_repos`` is a silent outage.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence

from viberpit import gitrepos

log = logging.getLogger(__name__)

GhRun = Callable[..., object]


def _owner_name(slug: str) -> tuple[str, str] | None:
    """``(owner, name)`` for a well-formed slug, else ``None``.

    Anything else is never sent to GitHub: one unsplittable value would build a
    malformed document and fail the whole batch, taking the valid slugs with it.
    """
    parts = (slug or "").strip().split("/")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        return None
    return parts[0].strip(), parts[1].strip()


def _build_query(pairs: Sequence[tuple[str, str]]) -> str:
    """One aliased document for every slug — N slugs cost one request, not N.

    Reads go through GraphQL against the shared 5,000-req/hr limit (repo norm),
    and a per-slug ``gh`` subprocess would be both slower and rate-limit hostile.
    """
    fields = "\n".join(
        f'  r{i}: repository(owner: "{owner}", name: "{name}") {{ nameWithOwner }}'
        for i, (owner, name) in enumerate(pairs)
    )
    return f"query {{\n{fields}\n}}"


def canonical_slugs(
    slugs: Sequence[str], *, gh_run: GhRun | None = None
) -> dict[str, str]:
    """Map each well-formed slug to GitHub's canonical ``nameWithOwner``.

    Slugs GitHub cannot resolve (deleted, typo'd, or invisible to this token) are
    simply absent from the result — the caller keeps the operator's text rather
    than guessing. Returns ``{}`` if the lookup could not be made at all.
    """
    # Read off the MODULE at call time, never bound at import: a module-level
    # ``from ... import _run_gh`` would freeze the seam for the whole xdist
    # worker and capture another test file's stub (the shard-only failure shape).
    if gh_run is None:
        gh_run = gitrepos._run_gh

    ordered: list[tuple[str, str]] = []
    originals: list[str] = []
    for slug in slugs:
        pair = _owner_name(slug)
        if pair is not None:
            ordered.append(pair)
            originals.append(slug)
    if not ordered:
        return {}

    query = _build_query(ordered)
    try:
        # stdin, not argv: a batched query can exceed MAX_ARG_STRLEN (128 KiB) and
        # make subprocess.run raise "Argument list too long" (test_gh_graphql_stdin).
        proc = gh_run(["api", "graphql", "-F", "query=@-"], input=query)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(
                getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "gh failed"
            )
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except Exception as exc:
        log.warning("repo_canon.lookup_failed — keeping slugs verbatim: %s", exc)
        return {}

    data = payload.get("data") or {}
    out: dict[str, str] = {}
    for i, original in enumerate(originals):
        node = data.get(f"r{i}")
        name = node.get("nameWithOwner") if isinstance(node, dict) else None
        if isinstance(name, str) and "/" in name:
            out[original] = name
    return out


def apply_canonical(repos: Sequence[str], mapping: dict[str, str]) -> list[str]:
    """*repos* rewritten through *mapping* and deduped — the pure half.

    Split out from :func:`canonicalize_repo_list` so a sweep over MANY stored
    lists can share one lookup (one query for the union of every row's slugs)
    without growing a second copy of the dedupe rule.

    Order is the operator's, and a canonical name inherits the position of the
    FIRST entry that resolved to it — so a rename never reshuffles the list. A
    blank entry is dropped; everything else survives, canonical or verbatim.
    """
    out: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        if not (repo or "").strip():
            continue
        canonical = mapping.get(repo, repo)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def canonicalize_repo_list(
    repos: Sequence[str], *, gh_run: GhRun | None = None
) -> list[str]:
    """*repos* with every alias replaced by its canonical name, deduped."""
    return apply_canonical(repos, canonical_slugs(repos, gh_run=gh_run))
