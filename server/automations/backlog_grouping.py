"""Background grouping optimizer for the Backlog Runner.

The runner used to pick and bundle issues INLINE at spawn time: a one-shot
title-only Haiku ranking whose top-K slice always bundled the maximum
``max_issues_per_pr`` issues, related or not. This module replaces that with a
precomputed GROUP PLAN: a background pass (driven by ``BacklogRunnerLoop``)
reads the full candidate pool — titles AND bodies — and asks Sonnet which
issues genuinely belong together in groups of 2..K. Singleton groups are the
expected norm; bundling needs positive evidence of relatedness. The model
lists ONLY the bundles: every candidate it leaves out is appended as a
singleton in queue-rank order, so the reply stays a few dozen entries no
matter how large the pool is. (Asking for a full partition of ~450 issues
overran the 64k output cap on half of all refreshes, and the truncated reply
was thrown away — #2217.)

The plan is persisted generation-at-a-time in the ``backlog_groups`` table
(surfaced read-only via ``GET /api/automations/backlog-runner/groups``); the
runner's selection path only ever POPS the next still-live group off the plan
— it never invents a grouping of its own. No plan → the runner waits and the
loop nudges a refresh, so the old always-max behavior cannot resurface. An
UNPARSEABLE reply still yields a plan (all singletons, the safe direction) but
is flagged ``degraded`` end to end — parser → store → API → runner event — so
it can never again pass for a real plan.
"""
from __future__ import annotations

import asyncio

import json
from dataclasses import dataclass
import logging
import os
from datetime import datetime, timezone
from typing import Any, Final

from server.sessions.account_auth import account_store_io, helper_env
from server.sessions.helper_call import helper_cwd, run_helper_call, stateless_helper_options
from server.sessions.claude_bin import cli_path_kwargs
from server.time_iso import iso_utc

from .backlog_selection import Batch, order_live_groups

log = logging.getLogger(__name__)

# Cap the per-issue body excerpt fed to the grouping prompt so a novel-length
# issue can't crowd the rest of the backlog out of the context window.
_BODY_EXCERPT_CHARS = 800

# Effort for the one-shot grouping call. At the default (high) Sonnet spent
# 30k–60k of a 64k output budget thinking before the JSON even started, and the
# reply was cut off mid-list (#2217). A fixed ``budget_tokens`` is NOT honored on
# Sonnet 5 — verified live 2026-09-22: 31k thinking tokens against a 12k budget
# — so the lever is adaptive thinking plus effort. On the same 120-issue slice,
# medium thought ~10k tokens (low: 0) and still found real bundles.
GROUPING_EFFORT: Final = "medium"

_GROUPING_SYSTEM = (
    "You are a backlog grouping optimizer. You receive the full list of open, "
    "eligible GitHub issues and find the ones that belong in the same "
    "pull request. A group holds issues that GENUINELY belong in one PR: same "
    "subsystem and a shared change surface, one issue depends on another, or "
    "they are facets of one underlying defect. Superficial similarity (same "
    "component name, same label, similar wording) is NOT enough. When in doubt, "
    "keep issues apart — an issue worked on its own is the normal, expected "
    "case. Only list groups of two or more issues; every issue you leave out is "
    "worked on its own."
)


def _grouping_prompt(candidates: list[dict], max_k: int) -> str:
    """The user prompt: every candidate (ref, title, body excerpt) plus the
    strict-JSON output contract. Bundles only — singletons are implied, and
    :func:`parse_group_plan` appends them in queue-rank order."""
    blocks = []
    for c in candidates:
        body = (c.get("body") or "").strip()
        if len(body) > _BODY_EXCERPT_CHARS:
            body = body[:_BODY_EXCERPT_CHARS] + "…"
        block = f"{c['ref']}  {c.get('title') or '(no title)'}"
        if body:
            block += f"\n{body}"
        blocks.append(block)
    issues = "\n\n".join(blocks)
    return (
        f"From the following issues, list ONLY the groups of 2 to {max_k} "
        f"issues that genuinely belong in one pull request. Do NOT list single "
        f"issues: every issue you leave out is worked on its own, and that is "
        f"the normal case. Order refs within a group so the primary issue comes "
        f'first. If nothing belongs together, reply {{"groups": []}}.\n\n'
        f"Issues:\n\n{issues}\n\n"
        f'Reply once, with ONLY this JSON, no code fences, no prose. Do not '
        f'revise or comment after the JSON:\n'
        f'{{"groups": [{{"refs": ["owner/repo#1", "owner/repo#2"], '
        f'"rationale": "why these belong together"}}]}}'
    )


@dataclass(frozen=True)
class ParsedPlan:
    """:func:`parse_group_plan`'s answer. ``degraded`` is True when the model's
    text was not a plan at all and ``groups`` is the all-singletons fallback —
    the shape is identical to a real plan, so the flag is the only tell."""

    groups: list[dict]
    degraded: bool = False


@dataclass(frozen=True)
class RefreshSummary:
    """What one plan refresh produced, for the runner loop's event: the stored
    group count and whether the generation is a parser fallback."""

    groups: int
    degraded: bool = False


def _last_groups_object(text: str) -> dict | None:
    """Recover a plan when the model second-guesses itself in the same reply
    (#2228): a clean ``{"groups": [...]}``, then prose ("Wait, let me
    reconsider…"), then a corrected object. ``json.loads`` rejects the whole
    reply as "Extra data" even though the model's own final answer is right
    there.

    Decodes from every ``{`` with :class:`json.JSONDecoder` and keeps the LAST
    dict whose ``groups`` is a list — the settled answer, since it follows any
    earlier draft. Prose brackets and unrelated objects are ignored. ``None``
    when nothing qualifies, so genuine garbage still degrades."""
    decoder = json.JSONDecoder()
    best: dict | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("groups"), list):
            best = obj
    return best


def parse_group_plan(raw: str, candidates: list[dict], max_k: int) -> ParsedPlan:
    """Pure: model text → validated plan ``[{"refs": [...], "rationale": ...}]``.

    Enforced invariants (the model is NOT trusted):
    - only known candidate refs survive; unknown refs are dropped;
    - groups are disjoint — a ref's first appearance wins;
    - a group is truncated to ``max_k`` refs; emptied groups vanish;
    - candidates the model did not place are appended as singleton groups in
      the order they arrived — the queue's own ranking — so the plan always
      covers the whole pool without re-deciding its priority. Under the
      bundle-only contract this is the NORMAL path for most of the pool, and
      an empty ``groups`` list is a real plan (nothing belongs together).

    Unparseable output degrades to the all-singletons plan, still in queue
    order — ungrouped work proceeds one issue at a time rather than mis-bundled,
    and in the order the queue asked for rather than one invented here — and is
    flagged ``degraded`` so the caller can say so (#2217).
    """
    by_ref = {c["ref"]: c for c in candidates}
    max_k = max(1, int(max_k))

    groups: list[dict] = []
    seen: set[str] = set()
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        # a fenced block may open with a language tag line ("json")
        first_nl = text.find("\n")
        if first_nl != -1 and text[:first_nl].strip().isalpha():
            text = text[first_nl + 1:]
    degraded = False
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        payload = _last_groups_object(text)

    if isinstance(payload, (dict, list)):
        raw_groups = payload.get("groups") if isinstance(payload, dict) else payload
        if not isinstance(raw_groups, list):
            raw_groups = []
            degraded = True
    else:
        raw_groups = []
        degraded = True

    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        refs = [r for r in (g.get("refs") or [])
                if isinstance(r, str) and r in by_ref and r not in seen]
        refs = refs[:max_k]
        if not refs:
            continue
        seen.update(refs)
        rationale = g.get("rationale")
        groups.append({
            "refs": refs,
            "rationale": rationale if isinstance(rationale, str) else None,
        })

    # Leftovers keep the order they arrived in, which IS the queue's ranking
    # (``server.pm.queue.rank_key``: urgency, then score, then age, then ref).
    # This used to re-sort them by ``(number, repo)`` — a second definition of
    # priority, in a consumer, that silently outranked the canonical one for
    # every row it touched. #1698 floated urgent groups over it (below), which
    # fixes the incident case; this fixes the cause, so a human-scored issue is
    # no longer reordered by issue number either.
    #
    # Determinism was the only thing the sort bought, and it was already paid
    # for: ``rank_key`` ends in ``ref``, so candidate order is a total order.
    for c in (c for c in candidates if c["ref"] not in seen):
        groups.append({"refs": [c["ref"]], "rationale": None})

    return ParsedPlan(groups=float_urgent_first(groups, candidates), degraded=degraded)


def float_urgent_first(groups: list[dict], candidates: list[dict]) -> list[dict]:
    """A group holding an urgent candidate leads the plan. Stable, pure, and THE
    one place the invariant is applied.

    Urgent work leads the plan, whatever the model said and whatever the leftover
    ordering is. The queue already ranks incidents first
    (``server.pm.queue.rank_key``), but the plan is what the runner actually pops,
    so the invariant has to survive this hand-off too — and every path that builds
    a plan works against it. A model free to order by "implementation priority" can
    rank an incident last; the degraded path sorts leftovers by issue NUMBER, where
    an incident is the newest issue in the pool and therefore always last; and the
    evidence half (:func:`family_groups`) is concatenated ahead of the model's, so
    an incident would sit behind a day's worth of auto-error reports. That is not
    hypothetical: two incidents reporting both admin environments down came out
    328th and 329th of 332.

    Stable, so floating urgent work never otherwise reshuffles the priority
    ordering it is applied to.
    """
    urgent_refs = {c["ref"] for c in candidates if c.get("urgent")}
    return sorted(groups, key=lambda g: not urgent_refs.intersection(g.get("refs") or ()))


def order_by_pool_rank(groups: list[dict], candidates: list[dict]) -> list[dict]:
    """Order an assembled plan by each group's BEST member's queue rank. Stable,
    pure, and the one place two independently-built halves of a plan are merged.

    The evidence half (:func:`family_groups`) and the model's half are built
    separately and then concatenated, which made the concatenation itself a
    priority decision: every evidence group led every implement group, so a
    ``Ready`` score-9 issue waited behind a day's worth of auto-filed error
    reports (one per dispatch cycle at the ``max_issues_per_pr`` default of 1).
    Spec 2026-09-18 §5 dispatches an evidence hold "as an ordinary candidate
    (same capacity, ranking, policy)" — ranking included, so the two halves
    INTERLEAVE here instead.

    *candidates* arrives in the queue's own ranking
    (``server.pm.queue.rank_key``: urgency, then score, then age, then ref), so a
    ref's index in it IS its rank; a group takes the lowest index it holds. Groups
    are disjoint, so the keys are distinct and the sort is a total order; a group
    whose refs are all unknown sorts last rather than first. This deliberately
    subordinates the grouping model's own "implementation priority" ordering to the
    queue's rank — the same call ``parse_group_plan``'s leftovers already make, and
    the reason a human score is not re-decided by a consumer.

    Apply :func:`float_urgent_first` AFTER this: urgent work leads the plan
    whatever the ranking says.
    """
    rank = {c["ref"]: i for i, c in enumerate(candidates)}
    last = len(rank)

    def key(g: dict) -> int:
        ranks = [rank[r] for r in (g.get("refs") or ()) if r in rank]
        return min(ranks) if ranks else last

    return sorted(groups, key=key)


def family_groups(candidates: list[dict], *,
                  max_k: int) -> "tuple[list[dict], list[dict]]":
    """Pure: split *candidates* into ``(evidence groups, everything else)``.

    Two rules of spec 2026-09-18 §5, and they are the same rule seen from two
    sides. An ``evidence``-held item dispatches as an INVESTIGATION, not as an
    implementation, so it can never ride a batch with implement work — one session
    cannot be given both prompts. And sibling reports of one error-triage
    fingerprint family (``server/pm/error_family.py``) are one defect, so they
    become one investigation instead of N sessions racing to close each other as
    duplicates.

    Bucketing is by ``family``; a candidate with none groups alone (a missing key
    is the absence of evidence of relatedness). Buckets keep the pool's order — the
    queue's own ranking — and a bucket larger than *max_k* spills into further
    groups of the same family right behind the first, so the per-dispatch issue cap
    still applies. The primary is each group's first ref, exactly as everywhere
    else in the plan.

    Returns groups in the SAME ``{"refs", "rationale"}`` shape ``parse_group_plan``
    produces and ``store_plan`` persists: the caller concatenates the two halves
    into one plan, so a second shape here would be a second plan format.
    """
    max_k = max(1, int(max_k))
    evidence = [c for c in candidates if c.get("hold_kind") == "evidence"]
    rest = [c for c in candidates if c.get("hold_kind") != "evidence"]

    buckets: dict[Any, tuple[str | None, list[str]]] = {}
    for i, c in enumerate(evidence):
        family = c.get("family") or None
        # A familyless item gets a key nothing else can collide with, so "no
        # family" never becomes a family of its own.
        key: Any = ("family", family) if family else ("solo", i)
        buckets.setdefault(key, (family, []))[1].append(c["ref"])

    groups: list[dict] = []
    for family, refs in buckets.values():
        for start in range(0, len(refs), max_k):
            groups.append({
                "refs": refs[start:start + max_k],
                "rationale": (f"error-triage family {family}: sibling reports "
                              f"investigated together" if family else
                              "auto-filed error report: investigated on its own"),
            })
    return groups, rest


class GroupingModelError(RuntimeError):
    """The grouping model call itself failed (SDK/transport/auth), as opposed to
    the model answering with an unparseable plan. Carries the SDK's own message so
    the runner's ``groups_refresh_failed`` event can name the actual cause.
    ``kind`` is the classified failure kind (``failure.classify``), None when the
    call never ran, so the runner can pick its log level (#2623)."""

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy: resolved off the package at call
    # time, which lets server/tests/conftest.py stub every helper door at once (#1383)
    return query


async def propose_groups(
    candidates: list[dict],
    config: dict,
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
) -> ParsedPlan:
    """One-shot Sonnet call that finds the bundles in *candidates*.

    Returns the validated plan (see :func:`parse_group_plan`), ``degraded`` when
    the model answered with nothing parseable (logged here, the one place the
    raw reply is in hand), and raises :class:`GroupingModelError` when the model
    call ITSELF fails or is skipped (breaker open, account barred), carrying
    :meth:`HelperOutcome.describe` — classified kind plus the CLI's own words,
    never just the SDK's opaque exception — the caller treats that as a
    refresh failure and backs off, so a broken SDK never stores an empty plan
    over a real backlog. Tests inject ``_query_fn``; the conftest guard makes an
    un-injected call raise so no test ever reaches the real model.
    """
    if not candidates:
        return ParsedPlan(groups=[])

    max_k = max(1, int(config.get("max_issues_per_pr") or 1))
    query_fn = _query_fn or _default_sdk_query_fn()
    config_dir = _config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or helper_cwd()

    try:
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model="sonnet",
            system_prompt=_GROUPING_SYSTEM,
            cwd=cwd,
            env=await account_store_io(helper_env, config_dir),
            **stateless_helper_options(),
            thinking={"type": "adaptive"},
            effort=GROUPING_EFFORT,
            **(await asyncio.to_thread(cli_path_kwargs)),
        )
    except Exception as exc:
        raise GroupingModelError(str(exc)) from exc
    # Through the shared helper wrapper like every other one-shot call: it reads
    # the real cause off the stream (the bare exception is the opaque "returned
    # an error result: success"), and an org-denied account is barred rather
    # than re-called every refresh (#1845/#1846). No log here — the runner's
    # ``group_refresh_failed`` is the one record of a failed refresh.
    prompt = _grouping_prompt(candidates, max_k)
    outcome = await run_helper_call(
        lambda: query_fn(prompt=prompt, options=options),
        # One retry, not the default three: each attempt is a ~170k-token Sonnet
        # call, and a failed refresh already backs off and keeps its plan.
        subsystem="backlog_grouping", account_key=config_dir, retries=1)
    if outcome.skipped:
        raise GroupingModelError(f"call skipped: {outcome.skip_reason}")
    if outcome.error is not None:
        raise GroupingModelError(outcome.describe(),
                                 kind=outcome.failure_kind) from outcome.error
    text_parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                text_parts.append(t)
    raw = "".join(text_parts).strip()
    plan = parse_group_plan(raw, candidates, max_k)
    if plan.degraded:
        log.warning(
            "backlog_grouping: model reply unparseable (%d chars, starts %r) — plan "
            "degraded to all singletons for %d candidate(s)",
            len(raw), raw[:80], len(candidates))
    return plan


# --------------------------------------------------------------------------- #
# Plan persistence (backlog_groups table)                                      #
# --------------------------------------------------------------------------- #

async def store_plan(groups: list[dict], candidates: list[dict], *,
                     degraded: bool = False) -> int:
    """Persist *groups* as the new plan generation, REPLACING the previous one
    wholesale (consumed rows included — claimed issues are out of the candidate
    pool, so the new plan simply doesn't contain them). Returns the group count.
    ``degraded`` stamps every row of the generation (#2217) so the API can tell
    a parser fallback from a real plan.
    """
    from sqlalchemy import delete

    from server.db import db_session
    from server.db.models import BacklogGroup

    by_ref = {c["ref"]: c for c in candidates}
    now = iso_utc(datetime.now(timezone.utc))
    generation = f"gen-{now}"
    async with db_session() as db:
        await db.execute(delete(BacklogGroup))
        for rank, g in enumerate(groups):
            refs = list(g["refs"])
            primary = by_ref.get(refs[0]) or {}
            db.add(BacklogGroup(
                generation=generation,
                rank=rank,
                repo=primary.get("repo"),
                refs=refs,
                titles={r: t for r in refs
                        if (t := (by_ref.get(r) or {}).get("title"))},
                rationale=g.get("rationale"),
                created_at=now,
                degraded=degraded,
            ))
        await db.flush()
    return len(groups)


async def load_plan() -> list[dict]:
    """The current plan: unconsumed groups in priority (rank) order, as dicts
    ``{"id", "repo", "refs", "titles", "rationale"}``."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import BacklogGroup

    async with db_session() as db:
        rows = (await db.execute(
            select(BacklogGroup)
            .where(BacklogGroup.consumed_at.is_(None))
            .order_by(BacklogGroup.rank))).scalars().all()
        return [{
            "id": r.id,
            "repo": r.repo,
            "refs": list(r.refs or []),
            "titles": dict(r.titles or {}),
            "rationale": r.rationale,
        } for r in rows]


async def mark_consumed(group_id: int) -> None:
    """Stamp ``consumed_at`` on a claimed group (UI observability only)."""
    from sqlalchemy import update

    from server.db import db_session
    from server.db.models import BacklogGroup

    async with db_session() as db:
        await db.execute(update(BacklogGroup)
                         .where(BacklogGroup.id == group_id)
                         .values(consumed_at=iso_utc(datetime.now(timezone.utc))))


# --------------------------------------------------------------------------- #
# Plan consumption (pure)                                                      #
# --------------------------------------------------------------------------- #

def plan_to_batch(
    plan: list[dict],
    candidates: list[dict],
    repo_load: "dict[str, int] | None" = None,
    paused_repos: "set[str] | None" = None,
    *,
    strategy: str = "balanced",
    repo_order: "list[str] | None" = None,
    caps: "dict[str, int] | None" = None,
) -> "tuple[Batch | None, int | None]":
    """Pure: pop the highest-priority still-live group off *plan* as a Batch.

    A group is live when at least one of its refs is in the live candidate
    pool; stale refs (claimed/closed since the plan was computed) are dropped
    from the batch, never re-invented. The live groups are then ordered by the
    configured per-repo selection ``strategy`` (see
    :func:`~server.automations.backlog_selection.order_live_groups`) — ``balanced``
    (the default) reproduces the old ascending-active-run fairness; ``priority`` /
    ``concurrency-cap`` let the user rank or cap specific repos.
    Ordering is always stable, so plan priority stands within a repo.

    ``paused_repos`` (repos with stale git credentials, issue #670) are skipped
    entirely: branching a worktree off their stale ``origin`` would implement
    against ancient code, so the runner waits on those repos while still
    servicing every other repo's backlog.

    Returns ``(batch, group_id)`` or ``(None, None)`` when no plan group has a
    live ref, or when the strategy filtered every candidate repo out (a
    ``concurrency-cap`` where all repos with work are at their cap) — the
    runner then WAITS rather than choosing.
    """
    by_ref = {c["ref"]: c for c in candidates}
    paused = paused_repos or set()
    live: list[tuple[dict, list[str]]] = []
    for g in plan:
        refs = [r for r in g.get("refs") or [] if r in by_ref]
        if refs and by_ref[refs[0]]["repo"] not in paused:
            live.append((g, refs))
    if not live:
        return None, None
    live = order_live_groups(
        live, repo_of=lambda t: by_ref[t[1][0]]["repo"], strategy=strategy,
        repo_load=repo_load, repo_order=repo_order, caps=caps)
    if not live:
        return None, None
    group, refs = live[0]
    primary = refs[0]
    primary_hold_kind = by_ref[primary].get("hold_kind")
    # An evidence-held item dispatches as an investigation and can never ride a
    # batch with implement work (family_groups' invariant) — but the group's refs
    # were fixed when the PLAN was built while this hold_kind is read fresh at POP
    # time, so a primary that converted (or was parked) mid-plan must not carry
    # still-evidence-held siblings into an implement dispatch (#1963). Any ref
    # whose current evidence-classification disagrees with the primary's is
    # dropped from this batch; it stays in the live pool for the next refresh to
    # regroup correctly.
    primary_is_evidence = primary_hold_kind == "evidence"
    refs = [r for r in refs if r == primary
            or (by_ref[r].get("hold_kind") == "evidence") == primary_is_evidence]
    return Batch(
        primary=primary,
        issues=refs,
        repo=by_ref[primary]["repo"],
        titles={r: t for r in refs if (t := by_ref[r].get("title"))},
        # The primary's product decides the batch's policy (dispatch, attempt cap,
        # model). A group is single-product in practice — the plan is built per
        # board — and where it is not, the primary is the issue the session is
        # actually for, so its product is the right owner.
        product=by_ref[primary].get("product"),
        # Same reason the product rides along: the dispatch KIND is the primary's
        # question (spec 2026-09-18 §5), and the candidate dict is all the loop
        # receives across this hand-off. A secondary's hold does not travel — the
        # session is for the primary, and it is the primary's prompt that is seeded.
        primary_hold_kind=primary_hold_kind,
    ), group.get("id")


def group_view(row: Any) -> dict:
    """A ``BacklogGroup`` row as the camelCase dict the automations API serves."""
    return {
        "id": row.id,
        "repo": row.repo,
        "refs": list(row.refs or []),
        "titles": dict(row.titles or {}),
        "rationale": row.rationale,
        "rank": row.rank,
        "generation": row.generation,
        "createdAt": row.created_at,
        "consumedAt": row.consumed_at,
    }
