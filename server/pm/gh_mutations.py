"""GraphQL mutation documents for the 003 reconciler. Pure; shapes verified by Phase 0/B probes.

Note: GitHub's `rateLimit` is a Query-root field only — it cannot be embedded in a mutation
document (the live schema rejects it). Rate-limit info is fetched via a separate query
(`build_rate_limit_query` / `parse_rate_limit`), not appended to mutation documents.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .desired_state import LEGACY_STATUS_NEEDS_HUMAN, STATUS_HELD
from .reconcile_diff import Mutation

MAX_ALIASES = 20
ISSUE_FIELDS = "id title body state stateReason url"


def build_issue_query(node_id: str) -> str:
    return "query { node(id: %s) { ... on Issue { %s } } }" % (_q(node_id), ISSUE_FIELDS)


class UnknownOption(ValueError):
    """A single-select value has no option id in the field cache (board vocabulary drifted)."""


@dataclass(frozen=True)
class RateLimit:
    cost: int
    remaining: int
    reset_at: str | None


def _q(value: object) -> str:
    return json.dumps(value)  # quotes + escapes strings; numbers verbatim


def written_option_name(field: str | None, value: object,
                        field_cache: Mapping[str, dict]) -> str | None:
    """The single-select option name this write will actually land on, or None.

    THE write-side spelling resolver for the rename (spec 2026-09-18 §9): the engine
    only ever decides ``Held``, but a board the one-time rename has not reached still
    spells that column ``Needs human``. Same column — so the write uses whichever
    spelling the board's field cache carries, preferring the new one when it has both,
    and never creates a second option (``gh_reconcile._missing_status_options``).

    ``_render`` looks up the option id under this name, and ``as_written`` stamps the
    journaled mutation with it, so what we recorded is what we sent — see ``as_written``
    for why that matters.

    Status-scoped on purpose: every other single-select (``Stage``) matches by name only.
    """
    options = (field_cache.get(field or "") or {}).get("options") or {}
    name = str(value)
    if name in options:
        return name
    if field == "Status" and value == STATUS_HELD and LEGACY_STATUS_NEEDS_HUMAN in options:
        return LEGACY_STATUS_NEEDS_HUMAN
    return None


def as_written(m: Mutation, field_cache: Mapping[str, dict]) -> Mutation:
    """*m* restated in the vocabulary of the board it is about to be sent to.

    The reconciler journals a mutation BEFORE sending it, and that journal row is
    what the echo-suppression key is built from (``journal.recent_own_writes`` →
    ``mirror_store.upsert_project_items``). On a board still spelled the legacy way a
    decision of ``Held`` lands on the ``Needs human`` option, so a row recorded as
    ``Held`` would never match the readback: our own write would be reported as a
    ``HumanEdit`` and every parked card would emit a phantom ``gh_human_edit``
    observation. Stamping the mutation with the resolved name keeps the journal, the
    ownership replay (``own_applied_values``) and the echo key all saying what we
    actually wrote.

    A value with no option at all is returned unchanged, so ``_render`` still raises
    ``UnknownOption`` and the batch parks on vocabulary drift exactly as before.

    ``key()`` moves with the value, which is what the parked/dry-run dedupe sets want:
    both are keyed on ``target_node_id`` too, so they are per board already, and a board
    resolves the same spelling on every tick."""
    if m.kind != "set_field" or m.field == "Score":
        return m
    name = written_option_name(m.field, m.new_value, field_cache)
    return m if name is None or name == m.new_value else replace(m, new_value=name)


def _render(m: Mutation, project_node_id: str, field_cache: Mapping[str, dict]) -> str:
    if m.kind == "set_field":
        spec = field_cache.get(m.field or "") or {}
        field_id = spec.get("field_id")
        if not field_id:
            raise UnknownOption(f"field {m.field!r} not in cache")
        if m.field == "Score":
            value = "{number: %s}" % float(m.new_value)  # type: ignore[arg-type]
        else:
            name = written_option_name(m.field, m.new_value, field_cache)
            option = (spec.get("options") or {}).get(name) if name is not None else None
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
    if m.kind == "remove_blocked_by":
        return ("removeBlockedBy(input: {issueId: %s, blockingIssueId: %s}) { clientMutationId }"
                % (_q(m.target_node_id), _q(m.new_value)))
    if m.kind in {"update_issue", "close_issue"}:
        if not isinstance(m.new_value, dict):
            raise ValueError("issue mutation requires named fields")
        if m.kind == "update_issue":
            return ("updateIssue(input: {id: %s, title: %s, body: %s}) { issue { %s } }"
                    % (_q(m.target_node_id), _q(m.new_value["title"]),
                       _q(m.new_value["body"]), ISSUE_FIELDS))
        reason = m.new_value["stateReason"]
        if reason not in {"COMPLETED", "NOT_PLANNED"}:
            raise ValueError("unsupported issue close reason")
        return ("closeIssue(input: {issueId: %s, stateReason: %s}) { issue { %s } }"
                % (_q(m.target_node_id), reason, ISSUE_FIELDS))
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


def payload_from_gh(proc: object, *, partial_ok: bool) -> dict:
    """Turn a ``gh api graphql`` result into the GraphQL payload, or raise.

    ``gh`` exits non-zero whenever the response carries an ``errors`` array — even
    when the other aliases in the document succeeded and ``data`` is valid. For a
    mutation batch that payload IS the result (`parse_batch` maps each error to its
    alias, and the reconciler parks a mutation on its second failure), so with
    ``partial_ok`` a non-zero exit whose stdout is a real GraphQL body is returned,
    not raised. Without it (mirror reads) the exit stays fatal: a ``data.node: null``
    next to a permission error would otherwise read as "board deleted" (#973).

    Anything that is not a GraphQL body — empty stdout, HTML, a REST error object
    such as ``{"message": "Bad credentials"}`` — is a transport failure and raises
    in both modes, with gh's stderr as the message (#1508).
    """
    stdout = getattr(proc, "stdout", "") or ""
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        payload = None
    is_graphql = isinstance(payload, dict) and ("data" in payload or "errors" in payload)
    if getattr(proc, "returncode", 1) == 0:
        return payload if isinstance(payload, dict) else {}
    if partial_ok and is_graphql:
        return payload
    raise RuntimeError(
        f"gh graphql failed: {(getattr(proc, 'stderr', '') or '').strip()[:500]}")


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


def _options(names: Sequence[str], ids: Mapping[str, str] | None = None) -> str:
    return ", ".join(
        '{%sname: %s, color: GRAY, description: ""}'
        % ("id: %s, " % _q((ids or {}).get(n)) if (ids or {}).get(n) else "", _q(n))
        for n in names)


def build_create_field(project_node_id: str, name: str, data_type: str,
                       options: Sequence[str] | None) -> str:
    extra = f", singleSelectOptions: [{_options(options)}]" if options else ""
    return ("mutation { createProjectV2Field(input: {projectId: %s, dataType: %s, name: %s%s}) "
            "{ projectV2Field { ... on ProjectV2FieldCommon { id name } "
            "... on ProjectV2SingleSelectField { id name options { id name } } } } }"
            % (_q(project_node_id), data_type, _q(name), extra))


def build_update_field_options(field_id: str, options: Sequence[str],
                               *, option_ids: Mapping[str, str] | None = None) -> str:
    """Replace a single-select's option set with ``options`` (the caller's union, in order).

    ``updateProjectV2Field`` REPLACES the option list wholesale, and an input entry without
    an ``id`` is a *new* option: verified on a scratch board (#987, ``spike_gh_projects_v2.py
    optionids``) that omitting ids regenerates every id — ``Todo`` went ``f75ad846`` →
    ``ac49461c`` — and every item parked on a regenerated option came back with a null
    value. Passing each preserved option's existing id (``option_ids``, the board's
    name → option-id map straight out of ``field_cache``) keeps ids stable and the
    assignments with them. Names absent from the map are created fresh, which is exactly
    what the appended spec options want."""
    return ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
            "{ projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } } } }"
            % (_q(field_id), _options(options, option_ids)))
