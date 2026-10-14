"""Hard tool-layer guard: a session never removes a ``hold:human`` label (#3088).

``hold:human`` (``pr_status.HUMAN_HOLD_LABEL``) says a human owns a PR's merge. Only
that human releases it. A session working cairnea/KinDash#19237 removed the label
from #19061 — draft legal documents awaiting the owner's DPIA signature — while
commenting on it; the repo's auto-merge bot then armed and merged the PR unreviewed.
That was the fourth bypass of the same hold in 16 hours, after the prompt-level fix
for fix/conflict dispatches (#3072), so this check sits below the prompt.

It runs as an SDK ``PreToolUse`` hook rather than inside ``can_use_tool``: the CLI
consults ``can_use_tool`` only when it would otherwise prompt, so an account-level
``allow`` rule or ``bypassPermissions`` skips it, while a ``PreToolUse`` hook fires
for every Bash call in every mode.

Matching is on command text, deliberately broad: the label name (or its URL-encoded
form) plus any removal verb. A false positive costs the session a re-phrase (put
prose that quotes such a command in a file); a false negative merges an unsigned
legal document.
"""
from __future__ import annotations

import re

from server.github.pr_status import HUMAN_HOLD_LABEL

# The label as it can appear in a command: plain, quoted, or URL-encoded in a REST
# path (``/issues/N/labels/hold%3Ahuman``).
_LABEL = re.compile(re.escape(HUMAN_HOLD_LABEL).replace(":", "(?::|%3a)"), re.IGNORECASE)

# Every way a gh/git command removes a label: ``gh pr|issue edit --remove-label``, a
# REST ``DELETE`` on the labels endpoint (``-X DELETE`` / ``--method DELETE``),
# ``gh label delete``, and the GraphQL ``removeLabelsFromLabelable`` mutation.
_REMOVAL = re.compile(
    r"--remove-label\b|\bdelete\b|\bremoveLabelsFromLabelable\b", re.IGNORECASE)

DENY_REASON = (
    f"Blocked: removing the `{HUMAN_HOLD_LABEL}` label is reserved for the human who "
    f"owns the PR's merge — a session may never remove it, even on the owner's GitHub "
    f"account and even as a side effect of other work. Leave the label in place. If the "
    f"hold is blocking your objective, finish everything else and hand back asking the "
    f"owner to release it themselves. (If this command only quotes such a command in "
    f"prose, write that text to a file and pass it with -F / --body-file.)"
)


def removes_human_hold(command: str) -> bool:
    """True when the shell *command* would (or plausibly would) remove the
    ``hold:human`` label from a PR or issue."""
    return bool(_LABEL.search(command)) and bool(_REMOVAL.search(command))


async def human_hold_pre_tool_use(input_data, tool_use_id=None, context=None) -> dict:
    """SDK ``PreToolUse`` hook: deny a Bash call that removes ``hold:human``;
    every other call passes through untouched (an empty output defers to the
    normal permission flow)."""
    tool_input = (input_data or {}).get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not removes_human_hold(command):
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": DENY_REASON,
    }}
