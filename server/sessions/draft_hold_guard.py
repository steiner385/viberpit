"""Hard tool-layer guard: a session never marks another session's draft PR ready.

A draft is a hold. cairnea/KinDash narrowed ``hold:human`` to owner sign-off PRs
(cairnea/KinDash#19408) and keeps everything else the owner wants to see first —
specs, plans, owner-review PRs — as a draft. ``gh pr ready N`` turns that hold into a
merge: the repo's auto-merge bot arms every ready PR within a minute. On 2026-10-14 a
KinDash session marked owner-review drafts ready just to get CI to run. The fix
dispatch prompt already forbade it (#3072), but only in prose, and only for that one
dispatch kind, so this check sits below the prompt the way ``human_hold_guard`` does.

What a session may still do:

* mark ready a draft IT opened — ViberPit's own completion workflow tells a session
  to ``gh pr ready`` its own PR when one slipped through as a draft;
* ``gh pr ready N --undo`` — converting to draft is the safe direction;
* mark ready a PR that is not a draft (a no-op).

Ownership is the caller's ``owns_pr(repo, number, head_ref)`` (the orchestrator's
``_session_opened_pr``: a successful ``gh pr create`` for that PR in the session's own
transcript, else the PR's head is the session's own worktree branch). Without one —
a client built outside the orchestrator — no PR is the session's own.

Command shapes: ``gh pr ready [<number>|<url>|<branch>] [-R repo]`` anywhere in the
shell command (including inside ``bash -c``/``eval`` strings, after a ``cd``), a REST
``gh api …/pulls/N`` call that sets ``draft=false``, and the GraphQL
``markPullRequestReadyForReview`` mutation. The mutation names a node ID the guard
cannot map back to a PR, so it is always denied — ``gh pr ready N`` is the path for a
session's own PR. Prose that merely quotes ``gh pr ready`` inside a quoted argument
(an ``echo`` or a ``--body``) is one shell token and passes; the mutation name is
matched on raw text, so prose quoting it is denied — the hold guard's trade: a false
positive costs a re-phrase (``--body-file``), a false negative merges a held PR.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Awaitable, Callable

from viberpit import gitrepos

from server.github.pr_status import has_human_hold
from server.sessions.artifacts import github_pr_ref

# (repo, number, head_ref) -> is this PR one the session opened?
OwnsPr = Callable[[str, int, str], Awaitable[bool]]

# The guard's own deadline, inside the SDK's 60 s per-matcher hook timeout: the CLI
# treats a timed-out hook as a non-blocking error and RUNS the tool, so a slow PR
# lookup or transcript replay must end in a deny here, never in that timeout.
GUARD_BUDGET_S = 45.0

_SEPARATORS = frozenset((";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"))
_MUTATION = re.compile(r"\bmarkPullRequestReadyForReview\b", re.IGNORECASE)
_RAW_READY = re.compile(r"\bgh\s+pr\s+ready\b")
_PULLS_PATH = re.compile(r"^/?repos/([^/\s]+/[^/\s]+)/pulls/(\d+)/?$")
_DRAFT_FALSE = re.compile(r"""^draft=false$|["']?draft["']?\s*:\s*false\b""", re.IGNORECASE)

_PROSE_ADVICE = (
    "(If this command only quotes such a command in prose, write that text to a file "
    "and pass it with -F / --body-file.)")

_HAND_BACK = (
    "A draft is a hold: whoever opened it decides when it becomes mergeable, and marking "
    "it ready lets the repo's auto-merge bot arm and merge it. Leave it a draft — push "
    "your work to it if that is your task. If the owner really wants it ready, finish "
    "everything else and hand back asking them to click \"Ready for review\" themselves. "
    "Converting a PR back to a draft (`gh pr ready <n> --undo`) is always allowed.")

MUTATION_REASON = (
    "Blocked: the GraphQL `markPullRequestReadyForReview` mutation names a PR by node ID, "
    "so this guard cannot tell whether this session opened it. For a draft THIS session "
    "opened, run `gh pr ready <number>` instead. " + _HAND_BACK + " " + _PROSE_ADVICE)

UNRESOLVED_REASON = (
    "Blocked: marking a draft PR ready is reserved for the session that opened it, and "
    "this guard could not identify which PR this command targets. Name it explicitly — "
    "`gh pr ready <number> --repo <owner/name>` — and retry. " + _HAND_BACK + " "
    + _PROSE_ADVICE)


def not_own_reason(repo: str, number: int) -> str:
    """The refusal for marking ready a draft this session did not open."""
    return (
        f"Blocked: {repo}#{number} is a draft this session did not open, and marking "
        f"another session's or the owner's draft ready is reserved for them — even on "
        f"the owner's GitHub account, even to get CI to run. {_HAND_BACK} (If you did "
        f"open it in this session, ownership is read from your `gh pr create` result: "
        f"retry once, and if it is still blocked, hand back.)")


@dataclass(frozen=True)
class ReadyCall:
    """One command that would mark a PR ready. *selector* is what ``gh pr view``
    takes (number, URL or branch; None = the current branch's PR), *repo* an
    explicit ``owner/name``, *cwd* a directory a preceding ``cd`` moved to."""
    selector: str | None
    repo: str | None
    cwd: str | None


@dataclass(frozen=True)
class Scan:
    calls: tuple[ReadyCall, ...] = ()
    deny_reason: str | None = None   # set when the command is refused outright


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _is_gh(token: str) -> bool:
    return os.path.basename(token) == "gh"


def _args(tokens: list[str], start: int) -> list[str]:
    out = []
    for tok in tokens[start:]:
        if tok in _SEPARATORS:
            break
        out.append(tok)
    return out


def _parse_pr_ready(args: list[str], cwd: str | None) -> ReadyCall | None:
    """The call ``gh pr ready <args>`` makes, or None when it cannot mark a draft
    ready (``--undo``, ``--help``)."""
    selector = repo = None
    it = iter(args)
    for arg in it:
        if arg == "--undo" or arg in ("-h", "--help"):
            return None
        if arg in ("-R", "--repo"):
            repo = next(it, None)
        elif arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif arg.startswith("-R") and len(arg) > 2:
            repo = arg[2:]
        elif not arg.startswith("-") and selector is None:
            selector = arg.lstrip("#") if arg.lstrip("#").isdigit() else arg
    return ReadyCall(selector, repo, cwd)


def _parse_api(args: list[str], cwd: str | None) -> ReadyCall | None:
    """A REST ``gh api`` call that sets ``draft=false`` on ``repos/o/r/pulls/N``."""
    path = next((m for m in map(_PULLS_PATH.match, args) if m), None)
    if path is None or not any(_DRAFT_FALSE.search(a) for a in args):
        return None
    return ReadyCall(path.group(2), path.group(1), cwd)


# (tokens, index of the token, cwd) -> the PR call a command starting there makes.
Matcher = Callable[[list[str], int, "str | None"], "ReadyCall | None"]


def _match_ready(tokens: list[str], i: int, cwd: str | None) -> ReadyCall | None:
    if not _is_gh(tokens[i]):
        return None
    if tokens[i + 1:i + 3] == ["pr", "ready"]:
        return _parse_pr_ready(_args(tokens, i + 3), cwd)
    if tokens[i + 1:i + 2] == ["api"]:
        return _parse_api(_args(tokens, i + 2), cwd)
    return None


def scan_tokens(tokens: list[str], match: Matcher, raw: re.Pattern[str],
                cwd: str | None = None, depth: int = 0) -> list[ReadyCall]:
    """Every PR call *match* finds in the shell *tokens*, following ``cd`` and the
    command strings of ``bash -c``/``eval``. An unparseable nested string that
    *raw* matches is an unresolvable call (fail closed). Shared with
    ``merge_hold_guard`` so both guards read a command the same way."""
    calls: list[ReadyCall] = []
    at_start = True
    for i, tok in enumerate(tokens):
        if tok in _SEPARATORS:
            at_start = True
            continue
        if at_start and tok == "cd" and i + 1 < len(tokens) \
                and tokens[i + 1] not in _SEPARATORS:
            target = os.path.expanduser(tokens[i + 1])
            cwd = target if cwd is None else os.path.join(cwd, target)
        at_start = False
        if depth < 3 and i > 0 and tokens[i - 1] in ("-c", "eval") \
                and any(c.isspace() for c in tok):
            # A command string a shell will run: `bash -c "…"`, `eval "…"`.
            try:
                calls += scan_tokens(_tokens(tok), match, raw, cwd, depth + 1)
            except ValueError:
                if raw.search(tok):
                    calls.append(ReadyCall(None, None, cwd))
            continue
        call = match(tokens, i, cwd)
        if call is not None:
            calls.append(call)
    return calls


_GH_REPO_ENV = re.compile(r"\bGH_REPO\b")


def env_repo_ambiguous(command: str, calls) -> bool:
    """True when ``GH_REPO`` appears in *command* and some call names no ``--repo``:
    gh would then resolve the PR in ``GH_REPO`` while the guard resolves it in the
    working directory — two different PRs. An explicit ``--repo`` wins over the
    variable in gh, so only those calls are safe to judge."""
    return bool(_GH_REPO_ENV.search(command)) and any(c.repo is None for c in calls)


def scan_command(command: str) -> Scan:
    """Every way the shell *command* would mark a PR ready."""
    if _MUTATION.search(command):
        return Scan(deny_reason=MUTATION_REASON)
    try:
        tokens = _tokens(command)
    except ValueError:
        # Unbalanced quoting: no reliable tokens, so judge the raw text (fail closed).
        if any("--undo" not in command[m.end():].split("\n", 1)[0]
               for m in _RAW_READY.finditer(command)):
            return Scan(deny_reason=UNRESOLVED_REASON)
        return Scan()
    calls = tuple(scan_tokens(tokens, _match_ready, _RAW_READY))
    if env_repo_ambiguous(command, calls):
        return Scan(deny_reason=UNRESOLVED_REASON)
    return Scan(calls=calls)


@dataclass(frozen=True)
class TargetPr:
    repo: str
    number: int
    head_ref: str
    is_draft: bool
    human_hold: bool = False   # carries ``hold:human`` (``pr_status.has_human_hold``)


def resolve_pr(call: ReadyCall, cwd: str | None) -> TargetPr | None:
    """Which PR *call* targets, resolved the way ``gh`` would resolve it — in the
    directory the command runs in, so an unqualified number or the current branch
    means the same PR to the guard as to the session (blocking; off the loop)."""
    args = ["gh", "pr", "view"] + ([call.selector] if call.selector else [])
    if call.repo:
        args += ["-R", call.repo]
    args += ["--json", "url,number,headRefName,isDraft,labels"]
    env = gitrepos._gh_env()
    token = gitrepos.app_token_for_owner(call.repo.partition("/")[0]) if call.repo else None
    if token:
        env["GH_TOKEN"] = token
    run_in = cwd if cwd and os.path.isdir(cwd) else None
    if run_in is None and not call.repo:
        return None  # e.g. `cd $DIR`: gh would read the daemon's repo, not the session's
    try:
        proc = subprocess.run(args, capture_output=True, text=True, env=env,
                              cwd=run_in, timeout=30)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ref = github_pr_ref(data.get("url") or "")
    if ref is None:
        return None
    labels = [lbl.get("name") for lbl in data.get("labels") or () if isinstance(lbl, dict)]
    return TargetPr(ref[0], ref[1], data.get("headRefName") or "", bool(data.get("isDraft")),
                    has_human_hold(labels))


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def draft_ready_guard(owns_pr: OwnsPr | None = None,
                      resolve: Callable[[ReadyCall, str | None], TargetPr | None] | None = None):
    """The SDK ``PreToolUse`` hook for one session: deny a Bash call that marks a
    draft PR ready unless *owns_pr* says this session opened it; every other call
    passes through untouched (an empty output defers to the normal permission flow)."""
    async def draft_ready_pre_tool_use(input_data, tool_use_id=None, context=None) -> dict:
        data = input_data or {}
        tool_input = data.get("tool_input") or {}
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return {}
        scan = scan_command(command)
        if scan.deny_reason:
            return _deny(scan.deny_reason)
        if not scan.calls:
            return {}
        base = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        try:
            return await asyncio.wait_for(_judge(scan.calls, base), GUARD_BUDGET_S)
        except asyncio.TimeoutError:
            return _deny(UNRESOLVED_REASON)

    async def _judge(calls: tuple[ReadyCall, ...], base: str | None) -> dict:
        for call in calls:
            cwd = os.path.join(base, call.cwd) if base and call.cwd else (call.cwd or base)
            pr = await asyncio.to_thread(resolve or resolve_pr, call, cwd)
            if pr is None:
                return _deny(UNRESOLVED_REASON)
            if not pr.is_draft:
                continue  # already ready: the command is a no-op
            if owns_pr is None or not await owns_pr(pr.repo, pr.number, pr.head_ref):
                return _deny(not_own_reason(pr.repo, pr.number))
        return {}
    return draft_ready_pre_tool_use
