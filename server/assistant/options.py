"""``ClaudeAgentOptions`` kwargs overlay for an assistant-kind session (spec §4.1).

Returned as a plain dict rather than a built ``ClaudeAgentOptions`` so it can be merged
over the options ``server/sessions/sdk_factory.real_sdk_factory`` already builds: the
factory owns the env, the permission plumbing and — critically — the resolved
``cli_path`` (see ``server/sessions/claude_bin.py``); this overlay owns only what makes
the session an assistant rather than a coding agent.

Two things this must NOT contain, both verified on SDK 0.2.105 + CLI 2.1.259
(docs/superpowers/spikes/2026-09-04-assistant-sdk-findings.md):

* ``setting_sources`` — ANY value hangs ``ClaudeSDKClient`` initialisation forever.
  Isolation from the operator's plugins/commands/settings comes instead from the
  link-less scratch ``CLAUDE_CONFIG_DIR`` built by
  ``server/sessions/scratch_config.helper_config_dir(..., share_links=False)``.
* ``cli_path`` — overriding it makes the SDK spawn its own vendored, older CLI
  (2.1.183), the shadowing bug ``claude_bin`` exists to prevent.
"""
from __future__ import annotations

from typing import Any

from .prompt import ASSISTANT_SYSTEM_PROMPT

# Built-ins denied by name in addition to ``tools=[]``: belt and braces, because the
# CLI's built-in tool set is not something the SDK lets us enumerate
# (``get_server_info()`` exposes no ``tools`` key on this CLI).
#
# "MultiEdit" is deliberately absent — it is not a tool on CLI 2.1.259, and naming it
# makes the CLI log `Permission deny rule "MultiEdit" matches no known tool`.
#
# SlashCommand / Skill / ExitPlanMode are here for the same reason: each is a lever back
# into the coding-agent surface (a project command, a skill's instructions, a plan-mode
# approval card) that the assistant has no business reaching.
DISALLOWED_BUILTINS: tuple[str, ...] = (
    "Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "KillShell", "BashOutput",
    "SlashCommand", "Skill", "ExitPlanMode",
)


def assistant_options(*, server: Any, max_turns: int, model: str) -> dict[str, Any]:
    """The options overlay for one assistant session.

    *server* is the in-process MCP server config from
    ``server.assistant.server.build_server`` (an ``McpSdkServerConfig`` mapping); it is
    mounted under the wire name ``ccswitch`` so the CLI sees ``mcp__ccswitch__*`` tools.
    """
    return {
        # A plain string REPLACES the factory's claude_code preset + ccswitch marker
        # append: the completion/PR/model-request marker contracts are coding-session
        # contracts and mean nothing here.
        "system_prompt": ASSISTANT_SYSTEM_PROMPT,
        "tools": [],
        "disallowed_tools": list(DISALLOWED_BUILTINS),
        "mcp_servers": {"ccswitch": server},
        # Hide the account's own configured MCP servers (the operator has ~10).
        "strict_mcp_config": True,
        "max_turns": max_turns,
        "model": model,
        "effort": "low",
    }
