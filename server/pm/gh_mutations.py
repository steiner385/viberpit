"""GraphQL mutation documents for the 003 reconciler. Pure; shapes verified by Phase 0/B probes.

Note: GitHub's `rateLimit` is a Query-root field only — it cannot be embedded in a mutation
document (the live schema rejects it). Rate-limit info is fetched via a separate query
(`build_rate_limit_query` / `parse_rate_limit`), not appended to mutation documents.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .reconcile_diff import Mutation

MAX_ALIASES = 20


class UnknownOption(ValueError):
    """A single-select value has no option id in the field cache (board vocabulary drifted)."""


@dataclass(frozen=True)
class RateLimit:
    cost: int
    remaining: int
    reset_at: str | None


def _q(value: object) -> str:
    return json.dumps(value)  # quotes + escapes strings; numbers verbatim


def _render(m: Mutation, project_node_id: str, field_cache: Mapping[str, dict]) -> str:
    if m.kind == "set_field":
        spec = field_cache.get(m.field or "") or {}
        field_id = spec.get("field_id")
        if not field_id:
            raise UnknownOption(f"field {m.field!r} not in cache")
        if m.field == "Score":
            value = "{number: %s}" % float(m.new_value)  # type: ignore[arg-type]
        else:
            option = (spec.get("options") or {}).get(str(m.new_value))
            if not option:
                raise UnknownOption(f"{m.field}={m.new_value!r} has no option id")
            value = "{singleSelectOptionId: %s}" % _q(option)
        return ("updateProjectV2ItemFieldValue(input: {projectId: %s, itemId: %s, fieldId: %s, "
                "value: %s}) { projectV2Item { id } }"
                % (_q(project_node_id), _q(m.target_node_id), _q(field_id), value))
    if m.kind == "add_item":
        return ("addProjectV2ItemById(input: {projectId: %s, contentId: %s}) { item { id } }"
                % (_q(m.target_node_id), _q(m.new_value)))
    if m.kind == "add_blocked_by":
        return ("addBlockedBy(input: {issueId: %s, blockingIssueId: %s}) { clientMutationId }"
                % (_q(m.target_node_id), _q(m.new_value)))
    raise ValueError(f"unknown mutation kind {m.kind!r}")


def build_batch(mutations: Sequence[Mutation], *, project_node_id: str,
                field_cache: Mapping[str, dict]) -> tuple[str, dict[str, Mutation]]:
    if len(mutations) > MAX_ALIASES:
        raise ValueError(f"batch of {len(mutations)} exceeds {MAX_ALIASES}")
    alias_map = {f"m{i}": m for i, m in enumerate(mutations)}
    body = " ".join(f"{alias}: {_render(m, project_node_id, field_cache)}"
                    for alias, m in alias_map.items())
    return f"mutation {{ {body} }}", alias_map


@dataclass(frozen=True)
class BatchResult:
    """Per-alias outcome of one batch, plus whether the *document* failed as a whole.

    The distinction is what keeps a blip from becoming permanent: GitHub reports a
    per-mutation rejection with a ``path`` naming the alias, but a 502, an abuse
    limit or a parse error arrives with no path and fails all 20 aliases at once.
    Only the first is evidence against a mutation, so only the first may count
    toward the reconciler's park-on-second-failure rule."""
    errors: dict[str, str | None]
    document_error: str | None = None

    def failed_transiently(self) -> bool:
        return self.document_error is not None


def parse_batch(payload: dict, alias_map: Mapping[str, Mutation]) -> BatchResult:
    data = payload.get("data") or {}
    by_alias: dict[str, str | None] = {alias: None for alias in alias_map}
    document_error: str | None = None
    for err in payload.get("errors") or []:
        path = err.get("path") or []
        alias = str(path[0]) if path else None
        message = str(err.get("message") or "error")
        if alias in by_alias:
            by_alias[alias] = message
        else:  # document-level error: fail every alias
            document_error = document_error or message
            for a in by_alias:
                by_alias[a] = by_alias[a] or message
    for alias in alias_map:
        if by_alias[alias] is None and data.get(alias) is None:
            by_alias[alias] = "no data returned"
    return BatchResult(by_alias, document_error)


def build_rate_limit_query() -> str:
    return "query { rateLimit { cost remaining resetAt } }"


def parse_rate_limit(payload: dict) -> RateLimit | None:
    data = payload.get("data") or {}
    rl = data.get("rateLimit") or None
    if not rl:
        return None
    return RateLimit(int(rl["cost"]), int(rl["remaining"]), rl.get("resetAt"))


def chunk(mutations: Sequence[Mutation], size: int = MAX_ALIASES) -> list[list[Mutation]]:
    return [list(mutations[i:i + size]) for i in range(0, len(mutations), size)]


def _options(names: Sequence[str]) -> str:
    return ", ".join('{name: %s, color: GRAY, description: ""}' % _q(n) for n in names)


def build_create_field(project_node_id: str, name: str, data_type: str,
                       options: Sequence[str] | None) -> str:
    extra = f", singleSelectOptions: [{_options(options)}]" if options else ""
    return ("mutation { createProjectV2Field(input: {projectId: %s, dataType: %s, name: %s%s}) "
            "{ projectV2Field { ... on ProjectV2FieldCommon { id name } "
            "... on ProjectV2SingleSelectField { id name options { id name } } } } }"
            % (_q(project_node_id), data_type, _q(name), extra))


def build_update_field_options(field_id: str, options: Sequence[str]) -> str:
    return ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
            "{ projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } } } }"
            % (_q(field_id), _options(options)))
