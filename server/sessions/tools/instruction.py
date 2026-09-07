"""The system-prompt paragraph that tells a coding session its ccswitch tools exist.
Detail lives in the tool descriptions, which the model reads at call time."""
from __future__ import annotations

SESSION_TOOLS_INSTRUCTION = (
    "ccswitch is the harness that dispatched and supervises this session; it exposes two "
    "MCP tools under the `ccswitch` server. `session_context` tells you how you were "
    "dispatched (repo, branch, originating automation and issue, goal). `report_feedback` "
    "reports a problem with ccswitch ITSELF — contradictory or wrong instructions, an "
    "ambiguous dispatch prompt, an interrupt or model/account switch at a bad moment, a "
    "broken worktree, credential or tool it provides — never a bug in the project you are "
    "working on or a choice the user made. One report per distinct problem, at most three "
    "per session. A report never waits for a reply: keep working."
)


def append_session_tools(base: str, *, enabled: bool) -> str:
    """*base* with the session-tools paragraph appended when the surface is mounted.

    THE one composition of "engine instructions + session-tools paragraph", called by
    both engines: ``claude_sdk_factory._build_append_string`` (the SDK's system-prompt
    append) and ``CodexAgentDriver.start``'s fresh ``thread/start``
    (``developerInstructions``). Two spellings of this join is how one engine would end
    up telling the model about tools it does not have — or, as happened first, not
    telling it about tools it does.

    ``enabled`` is the caller's already-resolved answer to "is the surface mounted for
    THIS session", not a re-read of the flag: the Claude path knows it from
    ``session_tools_enabled`` and the Codex path from whether a ``CodexSessionTools``
    was handed to the driver, and those must not be able to disagree with what was
    actually mounted.
    """
    if not enabled:
        return base
    return f"{base}\n\n{SESSION_TOOLS_INSTRUCTION}"
