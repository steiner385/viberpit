"""Release-milestone GitHub readings: one batched GraphQL query per ladder, and workflow streaks.

Pure code: building the query document and parsing its payload do no I/O (the refresh
runs them). Product-agnostic: nothing here names a product.

Alias scheme (stable for a given ladder, so a continuation query reuses it):
- ``s<i>``: top-level ``search(type: ISSUE) { issueCount }`` for an ``issue_count`` condition.
- ``repo<i>``: ``repository(owner:, name:)`` group holding that repo's ref and epic aliases.
- ``r<i>``: ``issueOrPullRequest(number:)`` for one ref of a ``refs_closed`` condition;
  parse aggregates a condition's refs into one ``{done, total}`` reading.
- ``e<i>``: ``issue(number:) { subIssues }`` for an epic. Each epic is fetched once, even
  when several milestones or ``sub_issues`` conditions name it; its reading is keyed by
  :func:`epic_reading_key` and its leaves by the epic's ref key.

``workflow_streak`` is not in the GraphQL query: the refresh reads runs with
``actions_api.list_workflow_runs`` and scores them with :func:`streak_from_runs`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

from server.pm.milestone_def import Condition, LadderDef, MilestoneDef, Ref
from server.pm.milestone_status import Reading
from server.time_iso import parse_iso_utc

NOT_FOUND = "not found or no access"
SUB_ISSUES_PAGE = 100
_MACRO_RE = re.compile(r"\{now-(\d+)d\}")
_REPO_QUALIFIER_RE = re.compile(r"(?:^|\s)repo:")  # a negated -repo: names no scope
_DONE_REF_STATES = frozenset({"CLOSED", "MERGED"})
_STREAK_IGNORED = frozenset({"skipped", "cancelled"})
_REF_FIELDS = "__typename ... on Issue { state url } ... on PullRequest { state url }"
_LEAF_FIELDS = (
    "number title url state repository { nameWithOwner } "
    "closedByPullRequestsReferences(first: 5, includeClosedPrs: false) { nodes { number url state } }"
)
# A milestone ref is a leaf and may be a PR (state OPEN/CLOSED/MERGED, no closing PRs).
_MREF_FIELDS = (
    f"__typename ... on Issue {{ {_LEAF_FIELDS} }} "
    "... on PullRequest { number title url state repository { nameWithOwner } }"
)


@dataclass(frozen=True)
class Target:
    """What one query alias measures.

    ``search``: ``(scope, key)`` is the condition (scope = milestone key or ``numbers``)
    and ``query`` its expanded query, for its ``source_url``.
    ``epic``: ``(scope, key)`` is :func:`epic_reading_key` of ``ref``.
    ``ref`` / ``mref``: one fetch per distinct ref; ``(scope, key)`` is
    :func:`ref_reading_key`, and ``reads`` lists the ``refs_closed`` conditions
    (``(scope, key)``) aggregated over it. ``mref`` is a milestone's ``refs`` entry,
    fetched with the leaf fields and returned as a one-item leaves list."""

    kind: str  # search | ref | mref | epic
    scope: str
    key: str
    ref: Ref | None = None
    query: str | None = None
    reads: tuple[tuple[str, str], ...] = ()


def epic_reading_key(ref: Ref) -> tuple[str, str]:
    """The readings key of an epic's ``{done, total}``: what a ``sub_issues`` condition reads."""
    return ("epic", f"epic:{ref.key}")


def ref_reading_key(ref: Ref) -> tuple[str, str]:
    """The readings key of a milestone ref's own ``{done, total: 1}`` (``mref`` targets)."""
    return ("ref", f"ref:{ref.key}")


def expand_query_macros(q: str, now: datetime) -> str:
    """Replace ``{now-<N>d}`` with the UTC date N days before ``now`` (``YYYY-MM-DD``)."""
    base = now.astimezone(timezone.utc) if now.tzinfo else now
    return _MACRO_RE.sub(lambda m: (base - timedelta(days=int(m[1]))).date().isoformat(), q)


def _gql_str(s: str) -> str:
    # A JSON string literal is a valid GraphQL string: quotes and backslashes escaped.
    return json.dumps(s)


def _search_query(cond: Condition, primary_repo: str, now: datetime) -> str:
    q = expand_query_macros(str(cond.measure.params["query"]), now)
    if _REPO_QUALIFIER_RE.search(q):
        return q
    return f"repo:{cond.measure.params.get('repo') or primary_repo} {q}"


def search_url(query: str) -> str:
    return f"https://github.com/search?q={quote_plus(query)}&type=issues"


def _sub_issues_epic(cond: Condition, ms: MilestoneDef | None) -> Ref | None:
    epic = cond.measure.params.get("epic")
    if isinstance(epic, Ref):
        return epic
    return ms.epics[0] if ms is not None and ms.epics else None


def _conditions(ladder: LadderDef) -> Iterable[tuple[str, MilestoneDef | None, Condition]]:
    for ms in ladder.milestones:
        for cond in ms.gate:
            yield ms.key, ms, cond
    for cond in ladder.numbers:
        yield "numbers", None, cond


def build_ladder_query(
    ladder: LadderDef,
    *,
    primary_repo: str,
    now: datetime,
    cursors: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Target]]:
    """One GraphQL document for every GitHub-read measure of ``ladder``, plus alias → target.

    With ``cursors`` (epic alias → ``endCursor`` from :func:`parse_ladder_payload`), the
    document is a continuation: only those epics, each from its cursor, with the same
    aliases as the full query."""
    targets: dict[str, Target] = {}
    searches: list[tuple[str, str]] = []
    epic_alias: dict[Ref, str] = {}
    by_repo: dict[str, list[tuple[str, int, str]]] = {}  # repo -> [(alias, number, fields)]

    def add_epic(ref: Ref) -> None:
        if ref in epic_alias:
            return
        alias = f"e{len(epic_alias)}"
        epic_alias[ref] = alias
        scope, key = epic_reading_key(ref)
        targets[alias] = Target("epic", scope, key, ref)
        after = f', after: {_gql_str(cursors[alias])}' if cursors and alias in cursors else ""
        fields = (
            f"url subIssues(first: {SUB_ISSUES_PAGE}{after}) {{ totalCount "
            f"pageInfo {{ hasNextPage endCursor }} nodes {{ {_LEAF_FIELDS} }} }}"
        )
        by_repo.setdefault(ref.repo, []).append((alias, ref.number, f"issue(number: {ref.number}) {{ {fields} }}"))

    ref_alias: dict[Ref, str] = {}
    ref_reads: dict[str, list[tuple[str, str]]] = {}

    def add_ref(ref: Ref, kind: str) -> str:
        if ref in ref_alias:
            return ref_alias[ref]
        alias = f"{kind[0]}{sum(1 for t in targets.values() if t.kind == kind)}"
        ref_alias[ref] = alias
        ref_reads[alias] = []
        targets[alias] = Target(kind, *ref_reading_key(ref), ref)
        fields = _MREF_FIELDS if kind == "mref" else _REF_FIELDS
        by_repo.setdefault(ref.repo, []).append(
            (alias, ref.number, f"issueOrPullRequest(number: {ref.number}) {{ {fields} }}")
        )
        return alias

    for milestone in ladder.milestones:
        for ref in milestone.epics:
            add_epic(ref)
    # Milestone refs first, so a ref a refs_closed condition also names is fetched once, as a leaf.
    for milestone in ladder.milestones:
        for ref in milestone.refs:
            add_ref(ref, "mref")
    for scope, ms, cond in _conditions(ladder):
        kind = cond.measure.kind
        if kind == "issue_count":
            alias = f"s{len(searches)}"
            q = _search_query(cond, primary_repo, now)
            searches.append((alias, q))
            targets[alias] = Target("search", scope, cond.key, query=q)
        elif kind == "refs_closed":
            for ref in cond.measure.params["refs"]:
                ref_reads[add_ref(ref, "ref")].append((scope, cond.key))
        elif kind == "sub_issues":
            epic = _sub_issues_epic(cond, ms)
            if epic is not None:
                add_epic(epic)

    for alias, reads in ref_reads.items():
        targets[alias] = replace(targets[alias], reads=tuple(dict.fromkeys(reads)))
    if cursors:
        targets = {a: t for a, t in targets.items() if a in cursors and t.kind == "epic"}
        searches = []
    lines = ["query {"]
    for alias, q in searches:
        lines.append(f"  {alias}: search(type: ISSUE, query: {_gql_str(q)}, first: 0) {{ issueCount }}")
    for i, (repo, items) in enumerate(by_repo.items()):
        items = [it for it in items if it[0] in targets]
        if not items:
            continue
        owner, _, name = repo.partition("/")
        lines.append(f"  repo{i}: repository(owner: {_gql_str(owner)}, name: {_gql_str(name)}) {{")
        lines.extend(f"    {alias}: {body}" for alias, _, body in items)
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines), targets


# ---------------------------------------------------------------- parsing


def _flatten(data: Mapping[str, Any]) -> dict[str, Any]:
    """Alias → node, lifting ref/epic aliases out of their ``repo<i>`` groups."""
    out: dict[str, Any] = {}
    for alias, node in data.items():
        if alias.startswith("repo") and isinstance(node, Mapping):
            out.update(node)
        else:
            out[alias] = node
    return out


def _errors_by_alias(payload: Mapping[str, Any]) -> dict[str, str]:
    """The GitHub error message for each alias named in a GraphQL ``errors[].path``."""
    out: dict[str, str] = {}
    for err in payload.get("errors") or ():
        if not isinstance(err, Mapping):
            continue
        for part in err.get("path") or ():
            if isinstance(part, str) and part not in out and err.get("message"):
                out[part] = str(err["message"])
    return out


def _leaf(node: Mapping[str, Any]) -> dict[str, Any]:
    prs = ((node.get("closedByPullRequestsReferences") or {}).get("nodes")) or []
    return {
        "repo": (node.get("repository") or {}).get("nameWithOwner"),
        "number": node.get("number"),
        "title": node.get("title"),
        "url": node.get("url"),
        "state": node.get("state"),
        # GitHub returns merged closing PRs too; only an OPEN one puts a leaf in flight.
        "prs": [{"number": p.get("number"), "url": p.get("url")} for p in prs if p and p.get("state") == "OPEN"],
    }


def leaves_done(leaves: Iterable[Mapping[str, Any]]) -> int:
    """How many leaves are done (closed, or a merged PR)."""
    return sum(1 for x in leaves if x.get("state") in _DONE_REF_STATES)


def parse_ladder_payload(
    payload: dict, targets: dict[str, Target], *, now: datetime
) -> tuple[dict[tuple[str, str], Reading], dict[str, list[dict]], dict[str, str]]:
    """Readings by ``(scope, key)``, leaf items by epic or milestone-ref key, next-page
    cursors by epic alias.

    ``payload`` is the full GraphQL response (``{"data": …, "errors": …}``). A missing or
    null alias is an error reading (the GitHub error when one names it)."""
    nodes = _flatten(payload.get("data") or {})
    errors = _errors_by_alias(payload)

    def err(alias: str) -> str:
        return errors.get(alias, NOT_FOUND)

    readings: dict[tuple[str, str], Reading] = {}
    leaves: dict[str, list[dict]] = {}
    cursors: dict[str, str] = {}
    ref_groups: dict[tuple[str, str], list[tuple[str, Target]]] = {}

    for alias, t in targets.items():
        node = nodes.get(alias)
        if t.kind in ("ref", "mref"):
            for cond_key in t.reads:
                ref_groups.setdefault(cond_key, []).append((alias, t))
            if t.kind == "mref":
                assert t.ref is not None
                if not isinstance(node, Mapping) or not node.get("state"):
                    readings[(t.scope, t.key)] = Reading(None, None, err(alias), None)
                    continue
                item = _leaf(node)
                leaves[t.ref.key] = [item]
                readings[(t.scope, t.key)] = Reading(
                    {"done": leaves_done([item]), "total": 1}, now, None, item["url"]
                )
            continue
        if t.kind == "search":
            if not isinstance(node, Mapping) or not isinstance(node.get("issueCount"), int):
                readings[(t.scope, t.key)] = Reading(None, None, err(alias), None)
                continue
            url = search_url(t.query) if t.query else None
            readings[(t.scope, t.key)] = Reading({"n": node["issueCount"]}, now, None, url)
            continue
        # epic
        assert t.ref is not None
        subs = node.get("subIssues") if isinstance(node, Mapping) else None
        if not isinstance(node, Mapping) or not isinstance(subs, Mapping):
            readings[(t.scope, t.key)] = Reading(None, None, err(alias), None)
            continue
        items = [_leaf(n) for n in subs.get("nodes") or () if isinstance(n, Mapping)]
        leaves[t.ref.key] = items
        page = subs.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursors[alias] = page["endCursor"]
        total = subs.get("totalCount")
        readings[(t.scope, t.key)] = Reading(
            {"done": leaves_done(items), "total": total if isinstance(total, int) else len(items)},
            now,
            None,
            node.get("url"),
        )

    for cond_key, group in ref_groups.items():
        missing = [t.ref.key for a, t in group if t.ref and not isinstance(nodes.get(a), Mapping)]
        if missing:
            first = next(a for a, t in group if t.ref and t.ref.key == missing[0])
            msg = err(first)
            readings[cond_key] = Reading(None, None, f"{', '.join(missing)}: {msg}", None)
            continue
        states = [(nodes[a].get("state"), nodes[a].get("url")) for a, _ in group]
        done = sum(1 for s, _ in states if s in _DONE_REF_STATES)
        open_urls = [u for s, u in states if s not in _DONE_REF_STATES]
        url = (open_urls or [states[0][1]])[0]
        readings[cond_key] = Reading({"done": done, "total": len(states)}, now, None, url)
    return readings, leaves, cursors


# ---------------------------------------------------------------- workflow streaks


def streak_from_runs(runs: list[dict]) -> tuple[int, datetime | None, str | None]:
    """Consecutive ``success`` count over newest-first runs, ignoring skipped, cancelled and
    unfinished runs. Returns ``(count, measured_at, url)``: the newest considered run's
    ``updated_at`` (naive UTC) and ``html_url``, or ``None``s when no run counts."""
    count = 0
    newest: dict | None = None
    for r in runs:
        if r.get("status") != "completed" or r.get("conclusion") in _STREAK_IGNORED or r.get("conclusion") is None:
            continue
        if newest is None:
            newest = r
        if r["conclusion"] != "success":
            break
        count += 1
    if newest is None:
        return 0, None, None
    at = parse_iso_utc(newest.get("updated_at"))
    return count, at.replace(tzinfo=None) if at else None, newest.get("html_url")
