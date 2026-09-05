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
# touches a protected path is allowed ONLY when every pipeline segment mentioning
# that path leads with one of these — anything unrecognised (python, perl, tee,
# cp, mv, truncate, dd, an unknown wrapper) is denied. Fail closed: the set of
# ways a shell can write a file is unbounded, so an allowlist is the only side
# that can be enumerated correctly.
_READ_ONLY_BINS = frozenset({
    "awk", "basename", "cat", "column", "comm", "cut", "diff", "dirname", "egrep",
    "fgrep", "file", "fold", "git", "grep", "head", "jq", "less", "ls", "md5sum",
    "more", "nl", "od", "realpath", "rg", "sed", "sha1sum", "sha256sum", "sort",
    "stat", "tail", "tr", "uniq", "wc", "xxd",
})
# `git` is broadly read-only, but these subcommands write to the working tree.
_GIT_WRITE_SUBCOMMANDS = frozenset({
    "apply", "checkout", "clean", "mv", "restore", "rm", "stash", "switch",
})
# In-place edit flags for the stream editors that are otherwise read-only.
_IN_PLACE_FLAG = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*i|--in-place)")
_STREAM_EDITORS = frozenset({"sed", "perl", "ruby"})
# Splits a command line into pipeline/sequence segments.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;&\n]")


def _is_prod_deploy(command: str) -> bool:
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


def _leading_binary(segment: str) -> str | None:
    """The command name a pipeline segment invokes, ignoring VAR=value prefixes."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None  # unbalanced quotes → unparseable → treat as unknown
    for token in tokens:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue  # environment assignment prefix
        return Path(token).name
    return None


def _protected_shell_target(command: str) -> str | None:
    """A protected path this shell command would write, or None.

    Deny patterns in ``MANAGED_DENY`` match tool invocations, not shell text, so
    ``sed -i``/``cat >``/``tee`` reach the same files unchecked. This closes that
    route for every agent wired to the hook, while keeping reads frictionless.
    """
    for path in _PROTECTED_EDIT_PATHS:
        if path not in command:
            continue
        # Redirection into the protected path, e.g. `cat > path`, `echo x >> path`.
        if re.search(r">>?\s*['\"]?" + re.escape(path), command):
            return path
        for segment in _SEGMENT_SPLIT.split(command):
            if path not in segment:
                continue
            binary = _leading_binary(segment)
            if binary is None or binary not in _READ_ONLY_BINS:
                return path
            if binary in _STREAM_EDITORS and _IN_PLACE_FLAG.search(segment):
                return path
            if binary == "git":
                tokens = [t for t in shlex.split(segment)[1:] if not t.startswith("-")]
                if tokens and tokens[0] in _GIT_WRITE_SUBCOMMANDS:
                    return path
    return None


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
