"""Production factory: builds a real ClaudeSDKClient for a SessionDriver."""
from __future__ import annotations
import json
import os

from .completion import COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION


def _build_append_string() -> str:
    return COMPLETION_INSTRUCTION + "\n\n" + NO_PR_INSTRUCTION

# Inline flag-settings payload (--settings layer, highest priority).  Auto-approves any
# project-scoped `.mcp.json` MCP server so the CLI's trust gate (LX1 → "pending" → prompt)
# never fires.  We inject it here rather than via a settings *file* because ccswitch does not
# pass `setting_sources`, so the binary loads no filesystem settings — only this inline layer.
_MCP_TRUST_SETTINGS = json.dumps({"enableAllProjectMcpServers": True})


def _import_sdk():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    return ClaudeSDKClient, ClaudeAgentOptions


def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
    ClaudeSDKClient, ClaudeAgentOptions = _import_sdk()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    if extra_env:
        env.update(extra_env)
    hooks = None
    if on_pre_compact is not None:
        from claude_agent_sdk import HookMatcher
        hooks = {"PreCompact": [HookMatcher(hooks=[on_pre_compact])]}
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=("plan" if mode == "plan" else "default"),
        include_partial_messages=True, resume=resume,
        hooks=hooks,
        settings=_MCP_TRUST_SETTINGS,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _build_append_string(),
        },
    )
    return ClaudeSDKClient(options=opts)


async def _deny_all_tools(tool_name, tool_input, ctx=None):
    from claude_agent_sdk import PermissionResultDeny
    return PermissionResultDeny(message="tools are disabled during suggestion generation")


def real_suggestion_factory(cwd, config_dir):
    """One-shot, resume-less client for generating next-prompt suggestions.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku but can be overridden (or cleared to the account default) via
    CCSWITCH_SUGGEST_MODEL.
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .suggestions import SUGGEST_SYSTEM_PROMPT
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    model = os.environ.get("CCSWITCH_SUGGEST_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUGGEST_SYSTEM_PROMPT, model=model, resume=None,
    )
    return ClaudeSDKClient(options=opts)


def real_summary_factory(cwd, config_dir):
    """One-shot, resume-less client for generating a session status summary.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku, overridable via CCSWITCH_SUMMARY_MODEL.
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .summaries import SUMMARY_SYSTEM_PROMPT
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    model = os.environ.get("CCSWITCH_SUMMARY_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUMMARY_SYSTEM_PROMPT, model=model, resume=None,
    )
    return ClaudeSDKClient(options=opts)
