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


#: GitHub's uniqueness validation when ``addBlockedBy`` is told to create a
#: blocked-by edge that is already on the issue — the full message is "An error
#: occurred while adding the blocking issue to the issue. Validation failed: Target
#: issue has already been taken" (verbatim from ``gh_write_journal``, 2026-09-20).
#: Matched as a substring, lowercased, because only the validation clause is stable.
_EDGE_ALREADY_EXISTS = "has already been taken"

#: Mutation kinds whose "you already did this" rejection means the desired state
#: holds. Deliberately just ``add_blocked_by``: it is the only rejection whose text
#: has been seen live, and inventing the wording for the others would turn a real
#: failure into a silent success. ``remove_blocked_by`` is NOT here — GitHub's reply
#: when the edge is already gone has never been observed.
#:
#: The field-bootstrap mutations (``create_field``, ``set_field_options``) never reach
#: here — they are sent singly, outside ``build_batch``/``parse_batch`` — and must not be
#: given a guessed entry either: ``gh_reconcile._converged_on_board`` settles their
#: "already true" by READING the board back, which needs no wording at all (#2114).
_IDEMPOTENT_REJECTIONS: Mapping[str, str] = {"add_blocked_by": _EDGE_ALREADY_EXISTS}


def _is_converged(m: Mutation, message: str) -> bool:
    """Is *message* GitHub telling us this mutation's effect is already in place?"""
    needle = _IDEMPOTENT_REJECTIONS.get(m.kind)
    return needle is not None and needle in message.lower()


@dataclass(frozen=True)
class BatchResult:
    """Per-alias outcome of one batch, plus whether the *document* failed as a whole.

    The distinction is what keeps a blip from becoming permanent: GitHub reports a
    per-mutation rejection with a ``path`` naming the alias, but a 502, an abuse
    limit or a parse error arrives with no path and fails all 20 aliases at once.
    Only the first is evidence against a mutation, so only the first may count
    toward the reconciler's park-on-second-failure rule.

    ``converged`` is the third outcome, and it is a *success*: the alias was rejected
    because what it asks for is already true on GitHub. The reconciler records those
    rows applied, which both keeps the park ladder for real defects and lands the key
    in ``journal.applied_keys`` so ``compile_edges`` stops re-planning the edge (#1947)."""
    errors: dict[str, str | None]
    document_error: str | None = None
    converged: frozenset[str] = frozenset()

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
    # Read off the settled verdicts, so an alias is converged only if the message it
    # ENDED with says so. A pathless failure judged no alias at all (#1508), so nothing
    # it says — including wording that would otherwise match — may pass as convergence.
    converged = frozenset() if document_error is not None else frozenset(
        a for a, msg in by_alias.items() if msg is not None and _is_converged(alias_map[a], msg))
    return BatchResult(by_alias, document_error, converged)


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


#: ``ProjectV2SingleSelectFieldOptionColor`` — the enum GitHub returns for an option's
#: colour and accepts back in the option input. Unquoted in GraphQL, so a value echoed
#: from a response is validated against this set rather than interpolated blind.
OPTION_COLORS: frozenset[str] = frozenset(
    {"GRAY", "BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE"})
#: What an option with no known colour gets: GitHub's own default for a new option.
DEFAULT_OPTION_COLOR = "GRAY"


def _options(names: Sequence[str], ids: Mapping[str, str] | None = None,
             attrs: Mapping[str, Mapping[str, object]] | None = None) -> str:
    """Render the ``singleSelectOptions`` input list.

    ``color`` and ``description`` are REQUIRED members of
    ``ProjectV2SingleSelectFieldOptionInput`` and the mutation replaces the option
    set wholesale, so an option rendered without them comes back gray with no
    description. That default is right for an option being *created* (the field
    bootstrap's appended spec names) and wrong for one being *preserved*: pass its
    current ``{color, description}`` in ``attrs`` — keyed by the name it will carry —
    and it keeps them."""
    out = []
    for name in names:
        oid = (ids or {}).get(name)
        attr = (attrs or {}).get(name) or {}
        color = str(attr.get("color") or "").upper()
        if color not in OPTION_COLORS:
            color = DEFAULT_OPTION_COLOR
        description = attr.get("description") or ""
        out.append('{%sname: %s, color: %s, description: %s}' % (
            "id: %s, " % _q(oid) if oid else "", _q(name), color,
            _q(description if isinstance(description, str) else "")))
    return ", ".join(out)


def build_create_field(project_node_id: str, name: str, data_type: str,
                       options: Sequence[str] | None) -> str:
    extra = f", singleSelectOptions: [{_options(options)}]" if options else ""
    return ("mutation { createProjectV2Field(input: {projectId: %s, dataType: %s, name: %s%s}) "
            "{ projectV2Field { ... on ProjectV2FieldCommon { id name } "
            "... on ProjectV2SingleSelectField { id name options { id name } } } } }"
            % (_q(project_node_id), data_type, _q(name), extra))


def build_update_field_options(field_id: str, options: Sequence[str], *,
                               option_ids: Mapping[str, str] | None = None,
                               option_attrs: Mapping[str, Mapping[str, object]] | None = None,
                               ) -> str:
    """Replace a single-select's option set with ``options`` (the caller's union, in order).

    ``updateProjectV2Field`` REPLACES the option list wholesale, and an input entry without
    an ``id`` is a *new* option: verified on a scratch board (#987, ``spike_gh_projects_v2.py
    optionids``) that omitting ids regenerates every id — ``Todo`` went ``f75ad846`` →
    ``ac49461c`` — and every item parked on a regenerated option came back with a null
    value. Passing each preserved option's existing id (``option_ids``, the board's
    name → option-id map straight out of ``field_cache``) keeps ids stable and the
    assignments with them. Names absent from the map are created fresh, which is exactly
    what the appended spec options want.

    ``option_attrs`` (name → ``{color, description}``) is the same story for the option's
    *appearance*: the input requires both, so an option resent without them is repainted
    gray with its description erased. A caller that holds the board's current values (a
    live fields read) passes them here and the preserved options come back unchanged; a
    caller that does not — the field bootstrap, appending brand-new names — passes nothing
    and gets GitHub's defaults, which is what a new option should get."""
    return ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
            "{ projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } } } }"
            % (_q(field_id), _options(options, option_ids, option_attrs)))
