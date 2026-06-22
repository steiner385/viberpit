"""Production factory: builds a real ClaudeSDKClient for a SessionDriver."""
from __future__ import annotations
import os

from .completion import COMPLETION_INSTRUCTION


def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=("plan" if mode == "plan" else "default"),
        include_partial_messages=True, resume=resume,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": COMPLETION_INSTRUCTION,
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
