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
