#!/usr/bin/env python3
"""PreToolUse approval gate — block a **prod deploy** unless it is authorized.

Phase 3 governance (issue #805, design §7). Wired in ``.claude/settings.json`` as
a ``PreToolUse`` hook on ``Bash``. It reads the standard hook event JSON on stdin
(``{tool_name, tool_input:{command}, ...}``); when the command looks like a
production deploy it consults the SDLC governance core:

    server.sdlc.governance.evaluate_release(environment="prod",
        release_approval=$RELEASE_APPROVAL, author=..., approver=...)

If the release is not approved it emits a ``deny`` decision with a clear reason and
the approval route (separation of duties: the agent that wrote the change cannot
set its own ``RELEASE_APPROVAL``). Any non-deploy command, or a deploy to a
non-prod environment, is allowed untouched.

**Fail-closed:** if the governance package can't be imported (e.g. a bare
worktree), a self-contained fallback still blocks a prod deploy that has no
``RELEASE_APPROVAL`` — the gate never fails *open*.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# A command is a *prod deploy* when it invokes a deploy verb AND targets prod.
# Kept deliberately narrow so ordinary git/log/grep commands mentioning "deploy"
# don't trip the gate.
_DEPLOY_VERB = re.compile(r"\b(deploy|release|rollout|promote)\b", re.IGNORECASE)
_PROD_TARGET = re.compile(r"(--?prod(uction)?\b|\bprod\b|\bproduction\b|ENV=prod)", re.IGNORECASE)


def _is_prod_deploy(command: str) -> bool:
    if os.environ.get("CCSWITCH_DEPLOY_ENV", "").lower() in ("prod", "production"):
        # An explicit deploy-env marker on a deploy command is authoritative.
        return bool(_DEPLOY_VERB.search(command))
    return bool(_DEPLOY_VERB.search(command) and _PROD_TARGET.search(command))


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
    if event.get("tool_name") != "Bash":
        _allow()
        return
    command = (event.get("tool_input") or {}).get("command") or ""
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
