"""Single source of truth for the session-completion marker.

The same literal is appended to every session's system prompt (so the agent
knows how to signal completion) and scanned for by the Translator (so the
dashboard can show 'Complete'). Keeping both in one module prevents drift.
"""
from __future__ import annotations

COMPLETION_MARKER = "<!-- ccswitch:complete -->"

COMPLETION_INSTRUCTION = (
    "When you have fully accomplished the objective of this session, end your "
    f"final message with `{COMPLETION_MARKER}` on its own line. Emit it ONLY "
    "when the work is genuinely complete — never for an interim step, and never "
    "while you are still working or waiting on something."
)
