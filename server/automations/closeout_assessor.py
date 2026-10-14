"""The close-out assessment's I/O half (design
``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md`` §3): gather what a
merged-PR close-out hold is about, ask a bounded stateless helper which of the
remaining items are done, agent-doable or a person's, and record the outcome.

A helper call, not a coding session: the model only CLASSIFIES; the needs-you hygiene
executor applies the outcome (``closeout_assess.decide``), so every write is
``risk_tolerance``-gated, dry-run-able and audited like the other hygiene rules —
free-form session tool calls would bypass all three.

Every GitHub read goes through the injected ``gh_run`` seam
(``backlog_github``'s pattern) and every model call through an injectable
``_query_fn``, so tests never touch the network or a model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

from viberpit import gitrepos
from server.automations import backlog_github
from server.pm import closeout_assess as ca
from server.pm.outcomes import body_fingerprint
from server.pm.queue import QueueRow
from server.sessions.account_auth import account_store_io, helper_env
from server.sessions.claude_bin import cli_path_kwargs
from server.sessions.helper_call import (helper_cwd, pick_helper_config_dir, run_helper_call,
                                         stateless_helper_options)
from server.time_iso import iso_utc

log = logging.getLogger(__name__)

#: The newest merged PRs that referenced the issue, read per assessment.
MAX_PRS = 3
#: Model and effort: classifying items against a diff, one short JSON reply.
ASSESS_MODEL: Final = "sonnet"
ASSESS_EFFORT: Final = "low"


def _gh_json(args: list[str], repo: str, *, gh_run: Any, what: str) -> Any | None:
    proc = gh_run(args, token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        stderr = str(getattr(proc, "stderr", "") or "")
        backlog_github._log_gh_failure(
            stderr, "closeout_assessor.gh_failed what=%s repo='%s' err=%s", what, repo,
            stderr.strip())
        return None
    try:
        return json.loads(getattr(proc, "stdout", "") or "null")
    except json.JSONDecodeError:
        log.info("closeout_assessor.gh_unparseable what=%s repo=%s", what, repo)
        return None


def _pr_detail(repo: str, node: Mapping, *, gh_run: Any) -> dict:
    """One merged PR's title, body, changed paths and a diff excerpt
    (``closeout_assess.DIFF_EXCERPT_MAX``). A failed read leaves that part empty:
    the title alone still tells the model something."""
    number = int(node.get("number") or 0)
    pr = _gh_json(["api", f"repos/{repo}/pulls/{number}"], repo, gh_run=gh_run,
                  what="pr") or {}
    files = _gh_json(["api", f"repos/{repo}/pulls/{number}/files?per_page=100"], repo,
                     gh_run=gh_run, what="pr_files") or []
    paths: list[str] = []
    hunks: list[str] = []
    size = 0
    for f in files if isinstance(files, list) else []:
        name = str((f or {}).get("filename") or "")
        if not name:
            continue
        paths.append(name)
        patch = str((f or {}).get("patch") or "")
        if patch and size < ca.DIFF_EXCERPT_MAX:
            hunk = f"--- {name}\n{patch}"
            hunks.append(hunk)
            size += len(hunk)
    return {"number": number, "title": str(pr.get("title") or node.get("title") or ""),
            "body": str(pr.get("body") or ""), "files": paths,
            "diff_excerpt": "\n".join(hunks)[:ca.DIFF_EXCERPT_MAX]}


def merged_prs(ref: str, *, gh_run: Any) -> list[dict] | None:
    """The newest ``MAX_PRS`` merged same-repo PRs that referenced *ref* (its
    cross-reference timeline, ``backlog_github._issue_crossref_pr_nodes``), each
    with its detail. ``None`` when the timeline read failed — unknown, not "none"."""
    nodes = backlog_github._issue_crossref_pr_nodes(ref, gh_run=gh_run)
    if nodes is None:
        return None
    repo = ref.partition("#")[0]
    seen: dict[int, Mapping] = {}
    for node in nodes:
        number = node.get("number")
        if node.get("state") == "MERGED" and isinstance(number, int):
            seen[number] = node
    newest = sorted(seen)[-MAX_PRS:]
    return [_pr_detail(repo, seen[n], gh_run=gh_run) for n in newest]


def _default_query_fn():
    from claude_agent_sdk import query  # lazy: resolved off the package at call time,
    # so server/tests/conftest.py's no-real-SDK guard closes this door too (#1383)
    return query


async def ask_helper(prompt: str, *, system_prompt: str, subsystem: str,
                     query_fn: Any = None, config_dir: str | None = None) -> str | None:
    """One bounded stateless helper call (``ASSESS_MODEL``, ``ASSESS_EFFORT``) — the
    needs-you hygiene loop's classifiers (this close-out assessment and the parked
    review, ``parked_reviewer``) share it. The model's reply text, or ``None`` when
    no call could be made (no account, the lane cooled down, the call failed) —
    retried on a later tick."""
    options = None
    if query_fn is None:
        cfg = config_dir or await account_store_io(pick_helper_config_dir, default=None)
        if not cfg:
            log.info("needs_you_hygiene.no_helper_account subsystem=%s", subsystem)
            return None
        config_dir = cfg
        from claude_agent_sdk import ClaudeAgentOptions
        query_fn = _default_query_fn()
        options = ClaudeAgentOptions(
            model=ASSESS_MODEL, system_prompt=system_prompt, cwd=helper_cwd(),
            env=await account_store_io(helper_env, config_dir),
            **stateless_helper_options(), thinking={"type": "adaptive"},
            effort=ASSESS_EFFORT, **(await asyncio.to_thread(cli_path_kwargs)))
    outcome = await run_helper_call(
        lambda: query_fn(prompt=prompt, options=options),
        subsystem=subsystem,
        account_key=config_dir or os.environ.get("CLAUDE_CONFIG_DIR", ""), retries=1)
    if outcome.skipped:
        return None
    if outcome.error is not None:
        log.log(outcome.log_level, "needs_you_hygiene.helper_call_failed subsystem=%s: %s",
                subsystem, outcome.describe())
        return None
    parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            parts.extend(t for t in (getattr(b, "text", None) for b in content)
                         if isinstance(t, str))
    return "".join(parts).strip()


async def assess(row: QueueRow, *, gh_run: Any, _query_fn: Any = None,
                 _config_dir: str | None = None,
                 now: datetime | None = None) -> dict | None:
    """Assess the close-out hold on *row* and answer its
    ``closeout_assess.assessment_state`` (``decision`` ``None`` for an unusable
    reply), or ``None`` when the inputs or the model could not be read."""
    prs = await asyncio.to_thread(merged_prs, row.ref, gh_run=gh_run)
    if prs is None:
        return None
    prompt = ca.build_prompt(row.title, row.body, backlog_github.unchecked_items(row.body), prs)
    reply = await ask_helper(prompt, system_prompt=ca.SYSTEM_PROMPT,
                             subsystem="needs_you_hygiene.closeout_assess",
                             query_fn=_query_fn, config_dir=_config_dir)
    if reply is None:
        return None
    assessment = ca.parse_verdict(reply)
    if assessment is None:
        # Expected now and then from a model; recorded so the same body is not
        # re-asked every tick. INFO: a WARNING would auto-file an issue.
        log.info("closeout_assessor.unparseable ref=%s chars=%d starts=%r", row.ref,
                 len(reply), reply[:80])
    return ca.assessment_state(
        fingerprint=body_fingerprint(row.title, row.body), decision=ca.decide(assessment),
        summary=assessment.summary if assessment is not None else "",
        at=str(iso_utc(now or datetime.now(timezone.utc))))


async def assess_and_record(ctx: Any, row: QueueRow, *, gh_run: Any, _query_fn: Any = None,
                            _config_dir: str | None = None) -> dict | None:
    """:func:`assess`, persisted as *ctx*'s target state for the issue (the
    needs-you hygiene automation's ``AutomationTargetState``, target = ref)."""
    state = await assess(row, gh_run=gh_run, _query_fn=_query_fn, _config_dir=_config_dir)
    if state is not None:
        await ctx.set_target_state(row.ref, state)
    return state
