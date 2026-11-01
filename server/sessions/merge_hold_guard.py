"""Hard tool-layer guard: a session never merges, or arms auto-merge on, a
``hold:human`` PR (#3101).

``hold:human`` (``pr_status.HUMAN_HOLD_LABEL``) says a human owns a PR's merge.
PR Doctor's MERGE CONFLICT dispatch armed cairnea/KinDash#19061 three times while it
carried the label, and armed #17519, which then merged with the label still on. The
dispatch prompt now leaves merge state alone (#3072), but only in prose — and a
session's own completion workflow says "arm auto-merge" — so this check sits below
the prompt the way ``human_hold_guard`` (label removal) and ``draft_hold_guard``
(``gh pr ready``) do. Unlike a draft, ownership does not matter: a held PR is the
human's to merge even when a session opened it.

Command shapes: ``gh pr merge [<number>|<url>|<branch>] [-R repo]`` in any form
(``--auto``, ``--admin``, an immediate merge) anywhere in the shell command, a REST
``gh api …/pulls/N/merge`` call, and the GraphQL ``enablePullRequestAutoMerge`` /
``mergePullRequest`` / ``enqueuePullRequest`` mutations. The mutations name a PR by
node ID. ``gh pr merge N`` covers the first two, so they are always denied; it cannot
jump a merge queue, so a lone ``enqueuePullRequest`` the guard can read exactly — one
top-level ``gh api graphql -f query='…'`` naming one ``pullRequestId`` as a literal or
a ``-f``/``-F`` variable bound once — is resolved to its PR and judged like any other
call (#3200: KinDash's owner-approved, lock-gated queue-jump recipe). Every other
shape of it is denied, and so is any ``gh api graphql`` call whose query the
guard cannot read (``query=@file``, ``--input``). A ``GH_REPO`` the guard would have
to evaluate the shell to apply denies a call that names no ``--repo``.
``gh pr merge --disable-auto`` is the safe direction and always passes. The scanner
and PR lookup are ``draft_hold_guard``'s.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Callable

from server.github.pr_status import HUMAN_HOLD_LABEL
from server.sessions import draft_hold_guard as _draft
from server.sessions.draft_hold_guard import ReadyCall, Scan, TargetPr

_MUTATION = re.compile(
    r"\b(?:enablePullRequestAutoMerge|mergePullRequest|enqueuePullRequest)\b",
    re.IGNORECASE)
_RAW_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
# A GraphQL query the guard cannot read: from a file or stdin (raw text, fail closed).
_GRAPHQL = re.compile(r"\bgraphql\b")
_HIDDEN_QUERY = re.compile(r"\bquery=@|--input\b")
# Relative or absolute (``https://api.github.com/…``), with or without a query string.
_MERGE_PATH = re.compile(
    r"^(?:https?://[^/\s]+)?/?repos/([^/\s]+/[^/\s]+)/pulls/(\d+)/merge/?(?:\?\S*)?$")
# `gh pr merge` flags that take a value, so the value is never read as the selector.
_VALUE_FLAGS = frozenset(("-b", "--body", "-F", "--body-file", "-t", "--subject",
                          "--match-head-commit", "-A", "--author-email"))

_HAND_BACK = (
    f"A `{HUMAN_HOLD_LABEL}` PR is merged by the human who owns it — never by a "
    f"session, even one that opened it, even on the owner's GitHub account, and even "
    f"when a completion workflow says to arm auto-merge. Push your work to it if that "
    f"is your task and leave its merge state alone; if the hold blocks your objective, "
    f"finish everything else and hand back asking the owner to merge or release it. "
    f"`gh pr merge <n> --disable-auto` is always allowed.")

_PROSE_ADVICE = (
    "(If this command only quotes such a command in prose, write that text to a file "
    "and pass it with -F / --body-file.)")

MUTATION_REASON = (
    "Blocked: a GraphQL merge / auto-merge mutation names a PR by node ID, so this guard "
    f"cannot tell whether it carries `{HUMAN_HOLD_LABEL}`. Use `gh pr merge <number>` "
    "instead. To jump a merge queue, run one `enqueuePullRequest` alone in a single "
    "top-level `gh api graphql -f query='…'` (single-quoted) whose input names "
    "`pullRequestId` once — as a literal or as a variable bound by one `-F id=PR_…` — and "
    "the guard checks that PR. " + _HAND_BACK + " " + _PROSE_ADVICE)

HIDDEN_QUERY_REASON = (
    "Blocked: this `gh api graphql` call reads its query from a file or stdin "
    "(`query=@…` / `--input`), so this guard cannot tell whether it merges a PR or "
    f"arms auto-merge on a `{HUMAN_HOLD_LABEL}` PR. Pass the query inline "
    "(`-f query='…'`), or use `gh pr merge <number>` to merge an unheld PR. "
    + _HAND_BACK)

UNRESOLVED_REASON = (
    f"Blocked: merging or arming auto-merge on a `{HUMAN_HOLD_LABEL}` PR is reserved for "
    "its owner, and this guard could not identify which PR this command targets. Name it "
    "explicitly — `gh pr merge <number> --repo <owner/name> …` — and retry. "
    + _HAND_BACK + " " + _PROSE_ADVICE)


def held_reason(repo: str, number: int) -> str:
    """The refusal for merging or arming a ``hold:human`` PR."""
    return f"Blocked: {repo}#{number} carries `{HUMAN_HOLD_LABEL}`. {_HAND_BACK}"


def _parse_pr_merge(args: list[str], cwd: str | None) -> ReadyCall | None:
    """The call ``gh pr merge <args>`` makes, or None when it cannot merge or arm
    (``--disable-auto``, ``--help``)."""
    selector = repo = None
    it = iter(args)
    for arg in it:
        if arg == "--disable-auto" or arg in ("-h", "--help"):
            return None
        if arg in ("-R", "--repo"):
            repo = next(it, None)
        elif arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif arg.startswith("-R") and len(arg) > 2:
            repo = arg[2:]
        elif arg in _VALUE_FLAGS:
            next(it, None)
        elif not arg.startswith("-") and selector is None:
            selector = arg.lstrip("#") if arg.lstrip("#").isdigit() else arg
    return ReadyCall(selector, repo, cwd)


def _parse_api(args: list[str], cwd: str | None) -> ReadyCall | None:
    """A REST ``gh api`` call on ``repos/o/r/pulls/N/merge``."""
    path = next((m for m in map(_MERGE_PATH.match, args) if m), None)
    return None if path is None else ReadyCall(path.group(2), path.group(1), cwd)


def _match_merge(tokens: list[str], i: int, cwd: str | None) -> ReadyCall | None:
    if not _draft._is_gh(tokens[i]):
        return None
    if tokens[i + 1:i + 3] == ["pr", "merge"]:
        return _parse_pr_merge(_draft._args(tokens, i + 3), cwd)
    if tokens[i + 1:i + 2] == ["api"]:
        return _parse_api(_draft._args(tokens, i + 2), cwd)
    return None


_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")


def _field(tok: str, it) -> str | None:
    """The ``name=value`` a ``gh api`` field flag at *tok* passes (consuming the next
    token when the value is separate), or None when *tok* is no field flag."""
    if tok in _FIELD_FLAGS:
        return next(it, "")
    for flag in _FIELD_FLAGS:   # attached: -Fquery=…, --field=query=…
        glued = flag + ("=" if flag.startswith("--") else "")
        if tok.startswith(glued) and len(tok) > len(glued):
            return tok[len(glued):]
    return None


def _hides_graphql_query(tokens: list[str]) -> bool:
    """True when a GraphQL call in *tokens* (after quote removal) takes its query
    from somewhere the guard cannot read: ``--input``, a ``query=@file`` field, or a
    query the shell builds at run time (``$VAR``, ``$(…)``, backticks)."""
    if not any(t == "graphql" or t.endswith("/graphql") for t in tokens):
        return False
    it = iter(tokens)
    for tok in it:
        if tok == "--input" or tok.startswith("--input="):
            return True
        field = _field(tok, it)
        if field is not None and field.startswith("query="):
            value = field[len("query="):]
            # GraphQL's own `$var`s sit inside a query; a value that IS an expansion
            # starts with `$`, and GraphQL never uses `$(` or backticks.
            if value.startswith(("@", "$")) or "$(" in value or "`" in value:
                return True
    return False


# The PR an `enqueuePullRequest` input names: a string literal or a `$variable`.
# GraphQL names are case-sensitive, so this is too (unlike `_MUTATION`).
_ENQUEUE_ID = re.compile(
    r"\benqueuePullRequest\s*\(\s*input\s*:\s*\{[^{}]*?\bpullRequestId\s*:\s*"
    r"(?:\"([^\"]*)\"|\$(\w+))")
# Opaque base64-ish; anything else ($VAR, @file, quotes, escapes) is unreadable.
_NODE_ID = re.compile(r"^[A-Za-z0-9_=+/-]+$")


def _enqueue_call(command: str, tokens: list[str]) -> ReadyCall | None:
    """The PR a lone merge-queue enqueue in *command* names, or None unless the guard
    can read exactly which node ID GitHub will receive (#3200). Fail closed: one
    merge-mutation token in the whole command, nested strings included; that token
    is the query field of a top-level ``gh api graphql`` call, passed verbatim in
    single quotes (so the shell expands nothing in it); one ``pullRequestId`` in the
    query, comments and strings included (so nothing hides which one is read); and
    the id is a literal
    or a variable bound exactly once to a plain node ID."""
    hits = [t for toks in _draft.token_lists(tokens) for t in toks if _MUTATION.search(t)]
    if len(hits) != 1 or not hits[0].startswith("query="):
        return None
    query = hits[0][len("query="):]
    if (len(_MUTATION.findall(query)) != 1
            or query.count("pullRequestId") != 1 or f"query='{query}'" not in command):
        return None
    named = _ENQUEUE_ID.search(query)
    if named is None:
        return None
    for i, tok in enumerate(tokens):
        if not (_draft._is_gh(tok) and tokens[i + 1:i + 3] == ["api", "graphql"]):
            continue
        args = _draft._args(tokens, i + 3)
        if any(a == "--hostname" or a.startswith("--hostname=") for a in args):
            return None   # the lookup reads github.com, not that host
        fields: list[tuple[str, str]] = []
        it = iter(args)
        for arg in it:
            field = _field(arg, it)
            if field is not None:
                name, _, value = field.partition("=")
                fields.append((name, value))
        queries = [v for n, v in fields if n == "query"]
        if not queries:
            continue
        if queries != [query]:
            return None
        literal, var = named.groups()
        bound = [literal] if literal is not None else [v for n, v in fields if n == var]
        if len(bound) != 1 or not _NODE_ID.match(bound[0]):
            return None
        return ReadyCall(None, None, None, bound[0])
    return None


def scan_command(command: str) -> Scan:
    """Every way the shell *command* would merge a PR or arm its auto-merge."""
    try:
        tokens = _draft._tokens(command)
    except ValueError:
        tokens = None
    enqueue = _enqueue_call(command, tokens) if tokens is not None else None
    if enqueue is None and _MUTATION.search(command):
        return Scan(deny_reason=MUTATION_REASON)
    if _GRAPHQL.search(command) and _HIDDEN_QUERY.search(command):
        return Scan(deny_reason=HIDDEN_QUERY_REASON)
    if tokens is None:
        if _GRAPHQL.search(command):   # an unreadable query: fail closed
            return Scan(deny_reason=HIDDEN_QUERY_REASON)
        # Unbalanced quoting: no reliable tokens, so judge the raw text (fail closed).
        if any("--disable-auto" not in command[m.end():].split("\n", 1)[0]
               for m in _RAW_MERGE.finditer(command)):
            return Scan(deny_reason=UNRESOLVED_REASON)
        return Scan()
    nested = _draft.token_lists(tokens)
    # quote-split, top level or nested: enable''PullRequest…
    if enqueue is None and any(_MUTATION.search(t) for toks in nested for t in toks):
        return Scan(deny_reason=MUTATION_REASON)
    if any(_hides_graphql_query(toks) for toks in nested):
        return Scan(deny_reason=HIDDEN_QUERY_REASON)
    calls = tuple(_draft.scan_tokens(tokens, _match_merge, _RAW_MERGE))
    if enqueue is not None:
        calls += (enqueue,)
    if _draft.env_repo_ambiguous(command, tokens, calls):
        return Scan(deny_reason=UNRESOLVED_REASON)
    return Scan(calls=calls)


def merge_hold_guard(resolve: Callable[[ReadyCall, str | None], TargetPr | None] | None = None):
    """The SDK ``PreToolUse`` hook: deny a Bash call that merges or arms a
    ``hold:human`` PR; every other call passes through untouched (an empty output
    defers to the normal permission flow)."""
    async def merge_hold_pre_tool_use(input_data, tool_use_id=None, context=None) -> dict:
        data = input_data or {}
        tool_input = data.get("tool_input") or {}
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return {}
        scan = scan_command(command)
        if scan.deny_reason:
            return _draft._deny(scan.deny_reason)
        if not scan.calls:
            return {}
        base = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        try:
            return await asyncio.wait_for(_judge(scan.calls, base), _draft.GUARD_BUDGET_S)
        except asyncio.TimeoutError:
            return _draft._deny(UNRESOLVED_REASON)

    async def _judge(calls: tuple[ReadyCall, ...], base: str | None) -> dict:
        for call in calls:
            cwd = os.path.join(base, call.cwd) if base and call.cwd else (call.cwd or base)
            pr = await asyncio.to_thread(resolve or _draft.resolve_pr, call, cwd)
            if pr is None:
                return _draft._deny(UNRESOLVED_REASON)
            if pr.human_hold:
                return _draft._deny(held_reason(pr.repo, pr.number))
        return {}
    return merge_hold_pre_tool_use
