"""Single source of truth for *which* `claude` executable ccswitch runs.

Used by BOTH the model picker (``model_catalog`` probes it for the alias list)
and the SDK session factory (``sdk_factory`` pins it as the client's ``cli_path``),
so the models the picker advertises and the binary that actually runs a session
can never diverge (constitution VI).

**Why this exists:** ``claude-agent-sdk >= 0.2.x`` bundles its own pinned
``claude`` CLI under ``claude_agent_sdk/_bundled/`` and its ``_find_cli()`` prefers
that bundle over the system binary. The bundle freezes at whatever CLI version the
SDK shipped (e.g. SDK 0.2.105 → CLI 2.1.183) and falls behind the Anthropic API's
minimum supported version, so every turn 400s with "Claude Code <old> does not
support this model; version <new> or newer is required". Pinning the SDK's
``cli_path`` to the auto-updating system ``claude`` keeps sessions current and in
lockstep with the picker's probe.

Resolution mirrors ``ccswitch.launcher.claude_argv``: honor ``CCSWITCH_CLAUDE_BIN``
(a full command whose first token is the executable), else the bare name
``"claude"`` resolved on ``PATH``.
"""
from __future__ import annotations

import os
import shlex
import shutil


def resolve_claude_bin() -> str | None:
    """Absolute path to the system ``claude`` executable, or ``None`` if none found.

    Returns ``None`` (rather than a bare name or bogus path) when nothing resolves,
    so callers can fall back to the SDK's own CLI discovery instead of crashing.
    """
    raw = os.environ.get("CCSWITCH_CLAUDE_BIN") or "claude"
    parts = shlex.split(raw)
    exe = parts[0] if parts else "claude"
    found = shutil.which(exe)
    if found:
        return found
    # An absolute/relative path that exists and is executable but which() misses
    # (e.g. not on PATH). Accept it directly.
    if os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe
    return None


def cli_path_kwargs() -> dict:
    """``{"cli_path": <system claude>}`` for ``ClaudeAgentOptions``, or ``{}``.

    Splat this into *every* ``ClaudeAgentOptions(...)`` in the codebase so no SDK
    client silently falls back to the stale bundled CLI. ``{}`` when the binary
    can't be resolved leaves ``cli_path`` at its default (None) → the SDK uses its
    own discovery rather than crashing on a bogus path.
    """
    binary = resolve_claude_bin()
    return {"cli_path": binary} if binary else {}
