"""Dispatch-time "wrong repository" check: one-shot Haiku call that says whether
a draft prompt looks like work for a repository other than the one selected.

The vocabulary the router judges against is the fleet's own recent history —
each repository's recent session titles — so "task modal" maps to KinDash
because that is where task-modal sessions ran, not because of any hand-kept
keyword list.

Fail-open: ``suggest_repo`` returns None on ANY failure (timeout, model
failure, unparseable reply, disabled by env) and ``recent_repo_vocabulary``
returns ``[]`` on any DB error. The caller (POST /api/dispatch/repo-check)
offers a switch only when a suggestion is non-None.
"""
from __future__ import annotations

import asyncio

import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select

from server.config import env_float
from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import Session
from server.pm.fenced_json import FencedJsonError, extract_fenced_json
from server.sessions.account_auth import account_store_io, helper_env
from server.sessions.claude_bin import cli_path_kwargs
from server.sessions.helper_call import helper_cwd, pick_helper_config_dir, run_helper_call, stateless_helper_options
from server.sessions.types import ASSISTANT_KIND

log = logging.getLogger(__name__)

#: Longest draft prompt the router sees; anything beyond is truncated.
PROMPT_CAP = 4000
#: Fallback vocabulary entry when a session has no title: a prefix of its first prompt.
PROMPT_TITLE_CHARS = 80
#: Most recent session rows consulted when building the vocabulary.
HISTORY_ROW_CAP = 2000

_ENABLED_ENV = "VIBERPIT_DISPATCH_REPO_CHECK_ENABLED"
_TIMEOUT_ENV = "VIBERPIT_REPO_CHECK_TIMEOUT_S"
_DEFAULT_TIMEOUT_S = 20.0

_SYSTEM = (
    "You are a router for a coding-agent fleet. You are given a draft prompt a "
    "user is about to dispatch, the repository currently selected for it, and "
    "the candidate repositories, each with the titles of its recent sessions. "
    "Decide which repository the prompt is about.\n"
    "Reply with ONLY a JSON object of the form "
    '{"repo": "<owner/name>" | null, "confidence": "high" | "low", '
    '"reason": "<one sentence, at most 20 words>"}.\n'
    "Set repo to null when you are unsure or when the prompt fits the selected "
    "repository. Only name a different repository when the prompt clearly "
    "belongs to it."
)


@dataclass(frozen=True)
class RepoVocabulary:
    """One candidate repository and what recently ran there."""

    repo: str            # nameWithOwner
    titles: list[str]    # recent session titles, newest first, deduped


@dataclass(frozen=True)
class RepoSuggestion:
    """A high-confidence "this prompt is really <repo> work" verdict."""

    repo: str
    reason: str


def repo_check_enabled() -> bool:
    """Kill switch: ``VIBERPIT_DISPATCH_REPO_CHECK_ENABLED`` (default on)."""
    return os.environ.get(_ENABLED_ENV, "true").strip().lower() in ("1", "true", "yes", "on")


def _timeout_s() -> float:
    return env_float(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S, minimum=0.1)


async def recent_repo_vocabulary(
    *, days: int = 120, max_repos: int = 12, titles_per_repo: int = 8,
) -> list[RepoVocabulary]:
    """Recent session titles grouped by repository, most recently active repo first.

    Reads ``Session`` rows with a repository, created within *days*, excluding
    assistant-drawer sessions. Per repo the titles are distinct, newest first,
    with the first ``PROMPT_TITLE_CHARS`` of ``initial_prompt`` standing in for
    an empty title. Any DB error degrades to ``[]``.
    """
    try:
        async with db_session() as db:
            now = (await db.execute(select(db_utc_now()))).scalar_one()
            cutoff = now - timedelta(days=days)
            rows = (await db.execute(
                select(Session.repo, Session.title, Session.initial_prompt)
                .where(
                    Session.repo.is_not(None),
                    Session.repo != "",
                    Session.created_at >= cutoff,
                    # NULL kind is the ordinary coding session; SQL `!=` against a
                    # NULL column yields NULL, not true, so it needs its own leg.
                    or_(Session.kind.is_(None), Session.kind != ASSISTANT_KIND),
                )
                .order_by(Session.created_at.desc())
                # A hint never justifies scanning the whole table: the newest
                # rows carry the vocabulary that matters.
                .limit(HISTORY_ROW_CAP)
            )).all()
    except Exception as exc:
        log.warning("recent_repo_vocabulary: DB read failed — no vocabulary: %s", exc)
        return []

    # Rows arrive newest first, so dict insertion order is "repo by most recent
    # session" and each repo's title list is already newest first.
    by_repo: dict[str, list[str]] = {}
    for repo, title, initial_prompt in rows:
        titles = by_repo.setdefault(repo, [])
        label = (title or "").strip() or (initial_prompt or "").strip()[:PROMPT_TITLE_CHARS]
        if label and label not in titles and len(titles) < titles_per_repo:
            titles.append(label)
    return [
        RepoVocabulary(repo=repo, titles=titles)
        for repo, titles in list(by_repo.items())[:max_repos]
    ]


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy: resolved off the package at call
    # time, which lets server/tests/conftest.py stub every helper door at once (#1383)
    return query


def _build_body(prompt: str, selected_repo: str | None, vocabulary: list[RepoVocabulary]) -> str:
    lines = [f"Selected repository: {selected_repo or 'none'}", "", "Candidate repositories:"]
    for entry in vocabulary:
        lines.append(f"- {entry.repo}")
        if entry.titles:
            lines.append(f"    recent: {' | '.join(entry.titles)}")
    lines += ["", "Draft prompt:", "```", prompt, "```"]
    return "\n".join(lines)


async def suggest_repo(
    prompt: str,
    selected_repo: str | None,
    vocabulary: list[RepoVocabulary],
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
    _timeout: float | None = None,
) -> RepoSuggestion | None:
    """One-shot Haiku query: is *prompt* really work for a repo other than
    *selected_repo*? Returns a suggestion only for a high-confidence pick of a
    vocabulary repo that differs from the selected one; None otherwise.

    Mirrors ``refine_prompt``/``assess_model``: tool-less one-shot query with an
    injected ``_query_fn`` seam so tests never call the real model, and None on
    ANY failure so the dispatch bar degrades to "no hint".
    """
    if not repo_check_enabled():
        return None
    if not prompt or not prompt.strip():
        return None
    selected_key = (selected_repo or "").lower()
    known = {entry.repo.lower(): entry.repo for entry in vocabulary}
    if not any(key != selected_key for key in known):
        return None

    timeout = _timeout if _timeout is not None else _timeout_s()
    config_dir = _config_dir or await account_store_io(pick_helper_config_dir, default=None) or os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or helper_cwd()

    try:
        # SDK resolution stays INSIDE the try: an unimportable SDK must degrade
        # to None, never raise to the caller.
        query_fn = _query_fn or _default_sdk_query_fn()
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model="haiku",
            system_prompt=_SYSTEM,
            cwd=cwd,
            env=await account_store_io(helper_env, config_dir),
            **stateless_helper_options(),
            **(await asyncio.to_thread(cli_path_kwargs)),
        )
    except Exception as exc:
        log.warning("suggest_repo: setup failed: %s", exc)
        return None

    body = _build_body(prompt.strip()[:PROMPT_CAP], selected_repo, vocabulary)

    # retries=0: the check runs inline while the user waits on the dispatch bar;
    # one timeout-bounded attempt, then silence.
    outcome = await run_helper_call(
        lambda: query_fn(prompt=body, options=options),
        subsystem="suggest_repo",
        account_key=config_dir,
        per_attempt_timeout=timeout,
        retries=0,
    )
    if outcome.skipped:
        return None
    if outcome.error is not None:
        if outcome.failure_kind == "timeout":
            log.warning("suggest_repo: exceeded %.0fs — no suggestion", timeout)
        else:
            log.log(outcome.log_level, "suggest_repo: model call failed: %s", outcome.describe())
        return None

    text_parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                text_parts.append(t)
    raw = "".join(text_parts)

    try:
        parsed = extract_fenced_json(raw, shape="object")
    except FencedJsonError:
        log.warning("suggest_repo: unparseable reply %r — no suggestion", raw[:120])
        return None

    repo = parsed.get("repo")
    if not isinstance(repo, str) or parsed.get("confidence") != "high":
        return None
    canonical = known.get(repo.lower())
    if canonical is None or repo.lower() == selected_key:
        return None
    reason = parsed.get("reason")
    return RepoSuggestion(repo=canonical, reason=reason if isinstance(reason, str) else "")
