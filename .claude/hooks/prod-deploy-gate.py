#!/usr/bin/env python3
"""Cross-agent PreToolUse gate for protected edits and production deploys.

Phase 3 governance (issue #805, design §7). Wired in ``.claude/settings.json`` as
a ``PreToolUse`` hook on ``Bash`` and in ``.codex/config.toml`` for ``Bash`` and
``apply_patch``. It reads the shared hook event JSON on stdin
(``{tool_name, tool_input:{command}, ...}``); when the command looks like a
production deploy it consults the SDLC governance core:

    server.sdlc.governance.evaluate_release(environment="prod",
        release_approval=$RELEASE_APPROVAL, author=..., approver=...)

If the release is not approved it emits a ``deny`` decision with a clear reason and
the approval route (separation of duties: the agent that wrote the change cannot
set its own ``RELEASE_APPROVAL``). Codex patch calls are also denied when they
target constitution or managed-policy files protected by ``MANAGED_DENY``.

**Fail-closed:** if the governance package can't be imported (e.g. a bare
worktree), a self-contained fallback still blocks a prod deploy that has no
``RELEASE_APPROVAL`` — the gate never fails *open*.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

# A command is a *prod deploy* when it invokes a deploy verb AND targets prod.
# Kept deliberately narrow so ordinary git/log/grep commands mentioning "deploy"
# don't trip the gate.
_DEPLOY_VERB = re.compile(r"\b(deploy|release|rollout|promote)\b", re.IGNORECASE)
_PROD_TARGET = re.compile(r"(--?prod(uction)?\b|\bprod\b|\bproduction\b|ENV=prod)", re.IGNORECASE)
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_PATCH_MOVE_PATH = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)


def _load_protected_paths() -> frozenset[str]:
    """Protected paths from the governance core, with a fail-closed fallback.

    The hook must work from a bare worktree where ``server/`` may not be
    importable, so the literal set is duplicated here as a last resort only.
    ``test_hook_protected_paths_match_governance_core`` asserts the two agree.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from server.sdlc.governance import PROTECTED_EDIT_PATHS  # noqa: PLC0415
        return frozenset(PROTECTED_EDIT_PATHS)
    except Exception:  # pragma: no cover - bare-worktree fallback
        return frozenset({
            ".specify/memory/constitution.md",
            ".claude/managed-settings.json",
        })


_PROTECTED_EDIT_PATHS = _load_protected_paths()

# Shell binaries that cannot modify a file they are pointed at. A command that
# touches a protected path is allowed ONLY when every segment mentioning that
# path leads with one of these (or only annotates, see ``_MENTION_ONLY_*``) —
# anything unrecognised (python, perl, tee, cp, mv, truncate, dd, an unknown
# wrapper) is denied. Fail closed: the set of ways a shell can write a file is
# unbounded, so an allowlist is the only side that can be enumerated correctly.
# Deliberately absent because they CAN write (#3517): awk (`print > f`,
# `system()`), sed (`w f`, `e cmd`, `-i`), sort (`-o f`), uniq/xxd (an output
# operand), less (`-o logfile`), rg (`--pre cmd`).
_READ_ONLY_BINS = frozenset({
    "basename", "cat", "column", "comm", "cut", "diff", "dirname", "egrep",
    "fgrep", "file", "fold", "git", "grep", "head", "jq", "ls", "md5sum",
    "more", "nl", "od", "realpath", "sha1sum", "sha256sum", "stat", "tail",
    "tr", "wc",
})
# `git` subcommands that never write the working tree. An allowlist, like the
# binaries above: a denylist of writers (checkout, restore, reset, merge, am,
# checkout-index, …) can never be complete.
_READ_ONLY_GIT = frozenset({
    "annotate", "blame", "cat-file", "check-attr", "check-ignore", "describe",
    "diff", "grep", "log", "ls-files", "ls-tree", "rev-parse", "shortlog",
    "show", "status", "whatchanged",
})
# Git global options that consume the next token as their value.
_GIT_VALUE_OPTIONS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--config-env",
})
# Git options that run configured commands or write an output file, turning a
# read subcommand into a writer (`git -c diff.external=sh diff`, `--output=f`,
# `git grep -O<cmd>`). git accepts any unambiguous abbreviation of a long option
# (`--out=f`) and bundles short flags (`-iO<cmd>`), so `_git_option_unsafe`
# matches prefixes and letters, not spellings. A pager command can only arrive
# via `-c` or an env prefix, both refused, so `-p`/`--paginate` stay allowed.
_GIT_UNSAFE_LONG = (
    "config-env", "exec-path", "ext-diff", "open-files-in-pager", "output", "textconv",
)
_GIT_UNSAFE_SHORT = frozenset("cO")
# An output redirection inside a token (`>`, `>>`, `2>`, `x>f`), with its target
# when written attached. Searched, not anchored: the lexer keeps `echo x>f` as
# one word `x>f`. The target must follow without whitespace, so a quoted
# `"a > b"` in prose is not a redirect. `>|` and `&>` reach the lexer as `>` + a
# separator, which leaves the target leading a segment of its own — denied.
_REDIRECT = re.compile(r">>?(\S*)$")


# Commands that only read or annotate: the words in their arguments (a commit
# subject, an issue body, a file name) are *mentioned*, never *executed*. A
# segment led by one of these skips the verb+target heuristic (issue #832).
_MENTION_ONLY_BINS = frozenset({
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ls",
    "echo", "printf", "wc", "file", "stat", "diff", "nl", "journalctl",
})
_MENTION_ONLY_GIT = frozenset({
    "log", "show", "diff", "status", "grep", "blame", "shortlog", "rev-parse",
    "ls-files", "ls-tree", "describe", "branch", "commit", "tag", "notes",
})
_MENTION_ONLY_GH = frozenset({
    ("issue", "view"), ("issue", "list"), ("issue", "create"), ("issue", "comment"),
    ("issue", "edit"), ("pr", "view"), ("pr", "list"), ("pr", "create"),
    ("pr", "comment"), ("pr", "edit"), ("pr", "diff"), ("pr", "checks"),
})
_WRAPPERS = frozenset({"sudo", "env", "command", "nohup", "time", "nice", "exec"})
# A heredoc opener: `<<EOF`, `<<-'EOF'`, `<<"EOF"`. Group 1 = quote, 2 = delimiter.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SUBSTITUTION = re.compile(r"\$\(|`")


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies — inert prose — unless the shell would expand them.

    An unquoted delimiter lets ``$(...)``/backticks in the body execute, so that
    body stays in the text (fail closed); a quoted delimiter makes it literal.
    """
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        match = _HEREDOC.search(line)
        if not match:
            continue
        quoted, delim = bool(match.group(1)), match.group(2)
        body: list[str] = []
        while i < len(lines) and lines[i].strip() != delim:
            body.append(lines[i])
            i += 1
        i += 1  # the terminator line
        if not quoted and _SUBSTITUTION.search("\n".join(body)):
            kept.extend(body)
    return "\n".join(kept)


def _segments(command: str) -> list[list[str]] | None:
    """Quote-aware split into command segments (token lists); None if unparseable.

    It does not cut inside quotes, so a ``;`` in an issue body doesn't turn
    prose into a fake command — but an unquoted newline separates commands just
    as ``;`` does, so it is a separator too (``cat x\\ncp y z`` is two commands).
    """
    separators = ";&|()\n"
    lexer = shlex.shlex(command, posix=True, punctuation_chars=separators)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(ch in separators for ch in token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _git_subcommand(args: list[str]) -> str | None:
    """The subcommand of ``git <args>``, skipping global options and their values.

    ``git -C . checkout f`` must yield ``checkout``, not ``.`` (#3517).
    """
    it = iter(args)
    for arg in it:
        if arg in _GIT_VALUE_OPTIONS:
            next(it, None)  # option value
        elif not arg.startswith("-"):
            return arg
    return None


def _git_option_unsafe(arg: str) -> bool:
    """True when ``arg`` is (or abbreviates, or bundles) a git option in ``_GIT_UNSAFE_*``."""
    if arg.startswith("--"):
        name = arg[2:].split("=", 1)[0]
        return bool(name) and any(full.startswith(name) for full in _GIT_UNSAFE_LONG)
    if arg.startswith("-"):
        return bool(_GIT_UNSAFE_SHORT & set(arg[1:]))
    return False


def _command_words(tokens: list[str]) -> list[str]:
    """``tokens`` with ``VAR=value`` prefixes and wrappers (``sudo``, ``env`` …) peeled."""
    words = [t for t in tokens if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t)]
    while words and Path(words[0]).name in _WRAPPERS:
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return words


def _is_mention_only(tokens: list[str]) -> bool:
    words = _command_words(tokens)
    if not words:
        return False
    binary, rest = Path(words[0]).name, words[1:]
    if binary in _MENTION_ONLY_BINS:
        return True
    if binary == "git":
        return _git_subcommand(rest) in _MENTION_ONLY_GIT
    if binary == "gh":
        positional = [a for a in rest if not a.startswith("-")]
        return tuple(positional[:2]) in _MENTION_ONLY_GH
    return False


def _executable_text(command: str) -> str:
    """The part of ``command`` that can execute something, prose removed.

    Heredoc bodies are dropped and each read/annotate segment (``cat``,
    ``grep``, ``git log``, ``gh issue create`` …) is skipped — unless it embeds
    a command substitution, which could run anything. Anything unparseable is
    returned whole, so the gate never fails open.
    """
    stripped = _strip_heredoc_bodies(command)
    segments = _segments(stripped)
    if segments is None:
        return stripped
    parts = []
    for tokens in segments:
        if _is_mention_only(tokens) and not any(_SUBSTITUTION.search(t) for t in tokens):
            continue
        parts.append(" ".join(tokens))
    return "\n".join(parts)


def _is_prod_deploy(command: str) -> bool:
    command = _executable_text(command)
    if os.environ.get("CCSWITCH_DEPLOY_ENV", "").lower() in ("prod", "production"):
        # An explicit deploy-env marker on a deploy command is authoritative.
        return bool(_DEPLOY_VERB.search(command))
    return bool(_DEPLOY_VERB.search(command) and _PROD_TARGET.search(command))


def _protected_patch_target(command: str) -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    for raw_path in (*_PATCH_PATH.findall(command), *_PATCH_MOVE_PATH.findall(command)):
        candidate = Path(raw_path.strip())
        resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
        try:
            relative = resolved.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if relative in _PROTECTED_EDIT_PATHS:
            return relative
    return None


def _protected_file_path(raw_path: str) -> str | None:
    """Resolve a native edit tool's ``file_path`` against the protected set.

    ``Edit``/``Write`` describe their target with ``file_path``, not ``command``;
    inspecting ``command`` for them finds nothing and silently allows.
    """
    if not raw_path:
        return None
    repo_root = Path(__file__).resolve().parents[2]
    candidate = Path(raw_path.strip())
    resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    try:
        relative = resolved.relative_to(repo_root).as_posix()
    except ValueError:
        return None
    return relative if relative in _PROTECTED_EDIT_PATHS else None


def _segment_cannot_write(tokens: list[str]) -> bool:
    """True when a command segment provably only reads or annotates.

    Read-only binaries, read-only ``git`` subcommands without a command- or
    file-writing option, ``echo``/``printf`` (redirection is checked separately)
    and ``gh`` annotate calls. Anything embedding a command substitution, or
    not recognised, can write.
    """
    if any(_SUBSTITUTION.search(t) for t in tokens):
        return False
    # Stricter than `_command_words`: an env prefix can turn a reader into a
    # runner (`GIT_EXTERNAL_DIFF=x git diff`), and a wrapper option can carry a
    # whole command (`env -S '…'`), so neither is peeled away here.
    words = list(tokens)
    while words and Path(words[0]).name in _WRAPPERS:
        words = words[1:]
    if not words or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0].startswith("-"):
        return False
    binary, rest = Path(words[0]).name, words[1:]
    if binary == "git":
        return (
            _git_subcommand(rest) in _READ_ONLY_GIT | _MENTION_ONLY_GIT
            and not any(_git_option_unsafe(arg) for arg in rest)
        )
    if binary == "gh":
        return _is_mention_only(words)
    return binary in _READ_ONLY_BINS or binary in ("echo", "printf")


def _protected_shell_target(command: str) -> str | None:
    """A protected path this shell command would write, or None.

    Deny patterns in ``MANAGED_DENY`` match tool invocations, not shell text, so
    ``sed -i``/``cat >``/``tee`` reach the same files unchecked. This closes that
    route for every agent wired to the hook, while keeping reads frictionless.

    The gate engages on the file's *basename*, so ``cd .claude && cp x
    managed-settings.json``, ``..``-relative paths and ``"$dir/managed-settings.json"``
    are caught too. Shell text still can't be parsed exhaustively — a glob or a
    computed name evades any text check — so this is the early, friendly deny;
    the control that can't be bypassed is the ``amendment`` CI check
    (``server/sdlc/constitution_gate.py``), which fails any PR touching these
    paths without the owner's acknowledgment (#3517).
    """
    segments = _segments(command)
    for path in _PROTECTED_EDIT_PATHS:
        name = Path(path).name
        if name not in command:
            continue
        if segments is None:
            return path  # unparseable → fail closed
        for tokens in segments:
            if not any(name in t for t in tokens):
                continue
            if name in _redirect_targets(tokens) or not _segment_cannot_write(tokens):
                return path
    return None


def _redirect_targets(tokens: list[str]) -> str:
    """The words a segment redirects output into (``> f``, ``2>>f``, ``>| f``), joined."""
    targets = []
    for i, token in enumerate(tokens):
        match = _REDIRECT.search(token)
        if match:
            targets.append(match.group(1) or (tokens[i + 1] if i + 1 < len(tokens) else ""))
    return "\n".join(targets)


def _deny(reason: str, route: str) -> None:
    """Emit a PreToolUse deny decision and exit 0 (the decision is in the JSON)."""
    msg = reason if not route else f"{reason}\n\nHow to unblock: {route}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": msg,
        }
    }))
    sys.exit(0)


def _allow() -> None:
    sys.exit(0)


def _evaluate(release_approval, author, approver):
    """Delegate to the governance core; fall back to a fail-closed inline check."""
    try:
        # A live session runs with cwd at the repo root; make the package importable
        # from a bare checkout too.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from server.sdlc.governance import evaluate_release  # noqa: PLC0415
        d = evaluate_release(
            environment="prod", release_approval=release_approval,
            author=author, approver=approver,
        )
        return d.allowed, d.reason, d.route
    except Exception:  # pragma: no cover - defensive fallback path
        # Fail closed: no approval token → block; identity split can't be checked
        # here, so a distinct-approver decision is deferred to the importable path.
        if not (release_approval or "").strip():
            return (False, "prod release requires RELEASE_APPROVAL (unset or empty)",
                    "Set RELEASE_APPROVAL=<change/incident ref> after a human approves the release.")
        if author and approver and author == approver:
            return (False, "separation of duties: author cannot approve their own prod release",
                    "A different identity than the change author must approve the release.")
        return (True, "prod release approved", "")


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        _allow()  # unpar-seable event: don't wedge the session
        return
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command") or ""
    file_path = tool_input.get("file_path") or ""
    route = (
        "Ask a human maintainer to make the policy change through the deliberate "
        "governance process."
    )
    # Native edit tools carry `file_path`; Codex patch envelopes carry `command`.
    # Checking the wrong field silently allows — keep the two branches separate.
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        protected_path = _protected_file_path(file_path)
        if protected_path:
            _deny(
                f"agent edits to {protected_path} are forbidden by managed governance policy",
                route,
            )
        _allow()
        return
    if tool_name == "apply_patch":
        protected_path = _protected_patch_target(command)
        if protected_path:
            _deny(
                f"agent edits to {protected_path} are forbidden by managed governance policy",
                route,
            )
        _allow()
        return
    if tool_name != "Bash":
        _allow()
        return
    protected_path = _protected_shell_target(command)
    if protected_path:
        _deny(
            f"shell writes to {protected_path} are forbidden by managed governance policy",
            route,
        )
        return
    if not _is_prod_deploy(command):
        _allow()
        return
    allowed, reason, route = _evaluate(
        os.environ.get("RELEASE_APPROVAL"),
        os.environ.get("CCSWITCH_RELEASE_AUTHOR"),
        os.environ.get("CCSWITCH_RELEASE_APPROVER"),
    )
    if allowed:
        _allow()
    else:
        _deny(reason, route)


if __name__ == "__main__":
    main()
