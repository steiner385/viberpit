"""Background grouping optimizer for the Backlog Runner.

The runner used to pick and bundle issues INLINE at spawn time: a one-shot
title-only Haiku ranking whose top-K slice always bundled the maximum
``max_issues_per_pr`` issues, related or not. This module replaces that with a
precomputed GROUP PLAN: a background pass (driven by ``BacklogRunnerLoop``)
reads the full candidate pool — titles AND bodies — and asks Sonnet to
partition it into coherent groups of 1..K genuinely-related issues, in
priority order. Singleton groups are the expected norm; bundling needs
positive evidence of relatedness.

The plan is persisted generation-at-a-time in the ``backlog_groups`` table
(surfaced read-only via ``GET /api/automations/backlog-runner/groups``); the
runner's selection path only ever POPS the next still-live group off the plan
— it never invents a grouping of its own. No plan → the runner waits and the
loop nudges a refresh, so the old always-max behavior cannot resurface.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from server.time_iso import iso_utc

from .backlog_selection import Batch

log = logging.getLogger(__name__)

# Cap the per-issue body excerpt fed to the grouping prompt so a novel-length
# issue can't crowd the rest of the backlog out of the context window.
_BODY_EXCERPT_CHARS = 800

_GROUPING_SYSTEM = (
    "You are a backlog grouping optimizer. You receive the full list of open, "
    "eligible GitHub issues and partition it into pull-request-sized groups. "
    "A group holds issues that GENUINELY belong in the same PR: same subsystem "
    "and a shared change surface, one issue depends on another, or they are "
    "facets of one underlying defect. Superficial similarity (same component "
    "name, same label, similar wording) is NOT enough. When in doubt, keep "
    "issues apart — a singleton group is the normal, expected case. Order "
    "groups by implementation priority: highest value, lowest risk first."
)


def _grouping_prompt(candidates: list[dict], max_k: int) -> str:
    """The user prompt: every candidate (ref, title, body excerpt) plus the
    strict-JSON output contract."""
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
        f"Partition ALL of the following issues into groups of 1 to {max_k} "
        f"issues each. Only put issues in the same group when they genuinely "
        f"belong in one pull request; singletons are normal. Every issue must "
        f"appear in exactly one group. Order groups by implementation "
        f"priority (highest first) and order refs within a group so the "
        f"primary issue comes first.\n\n"
        f"Issues:\n\n{issues}\n\n"
        f'Reply with ONLY this JSON, no code fences, no prose:\n'
        f'{{"groups": [{{"refs": ["owner/repo#1"], "rationale": "why these '
        f'belong together (or a one-line priority note for singletons)"}}]}}'
    )


def parse_group_plan(raw: str, candidates: list[dict], max_k: int) -> list[dict]:
    """Pure: model text → validated plan ``[{"refs": [...], "rationale": ...}]``.

    Enforced invariants (the model is NOT trusted):
    - only known candidate refs survive; unknown refs are dropped;
    - groups are disjoint — a ref's first appearance wins;
    - a group is truncated to ``max_k`` refs; emptied groups vanish;
    - candidates the model failed to place are appended as singleton groups in
      ascending issue-number order, so the plan always covers the whole pool.

    Unparseable output degrades to the all-singletons plan (number order) —
    ungrouped work proceeds one issue at a time rather than mis-bundled.
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
    try:
        payload = json.loads(text)
        raw_groups = payload.get("groups") if isinstance(payload, dict) else payload
        if not isinstance(raw_groups, list):
            raw_groups = []
    except (json.JSONDecodeError, AttributeError):
        raw_groups = []

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

    leftovers = sorted(
        (c for c in candidates if c["ref"] not in seen),
        key=lambda c: (c["number"], c["repo"]),
    )
    for c in leftovers:
        groups.append({"refs": [c["ref"]], "rationale": None})
    return groups


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy import — not available in tests
    return query


async def propose_groups(
    candidates: list[dict],
    config: dict,
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
) -> list[dict]:
    """One-shot Sonnet call that partitions *candidates* into a group plan.

    Returns the validated plan (see :func:`parse_group_plan`) or ``[]`` when
    the model call itself fails — the caller treats empty-with-candidates as a
    refresh failure and backs off, so a broken SDK never stores an empty plan
    over a real backlog. Tests inject ``_query_fn``; the conftest guard makes
    an un-injected call return ``[]`` so no test ever reaches the real model.
    """
    if not candidates:
        return []

    max_k = max(1, int(config.get("max_issues_per_pr") or 1))
    query_fn = _query_fn or _default_sdk_query_fn()
    config_dir = _config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()

    try:
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model="sonnet",
            system_prompt=_GROUPING_SYSTEM,
            cwd=cwd,
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            allowed_tools=[],
            setting_sources=[],
        )
        text_parts: list[str] = []
        async for msg in query_fn(prompt=_grouping_prompt(candidates, max_k),
                                  options=options):
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    text_parts.append(t)
        raw = "".join(text_parts).strip()
    except Exception as exc:
        log.warning("backlog_grouping: model call failed: %s", exc)
        return []
    return parse_group_plan(raw, candidates, max_k)


# --------------------------------------------------------------------------- #
# Plan persistence (backlog_groups table)                                      #
# --------------------------------------------------------------------------- #

async def store_plan(groups: list[dict], candidates: list[dict]) -> int:
    """Persist *groups* as the new plan generation, REPLACING the previous one
    wholesale (consumed rows included — claimed issues are out of the candidate
    pool, so the new plan simply doesn't contain them). Returns the group count.
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
) -> "tuple[Batch | None, int | None]":
    """Pure: pop the highest-priority still-live group off *plan* as a Batch.

    A group is live when at least one of its refs is in the live candidate
    pool; stale refs (claimed/closed since the plan was computed) are dropped
    from the batch, never re-invented. ``repo_load`` stable-reorders live
    groups by ascending active-run count (per-repo fairness, mirroring the
    old selection path) while preserving plan priority within a repo.

    ``paused_repos`` (repos with stale git credentials, issue #670) are skipped
    entirely: branching a worktree off their stale ``origin`` would implement
    against ancient code, so the runner waits on those repos while still
    servicing every other repo's backlog.

    Returns ``(batch, group_id)`` or ``(None, None)`` when no plan group has
    a live ref — the runner then WAITS for the grouper rather than choosing.
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
    if repo_load:
        live = sorted(live, key=lambda t: repo_load.get(by_ref[t[1][0]]["repo"], 0))
    group, refs = live[0]
    primary = refs[0]
    return Batch(
        primary=primary,
        issues=refs,
        repo=by_ref[primary]["repo"],
        titles={r: t for r in refs if (t := by_ref[r].get("title"))},
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
