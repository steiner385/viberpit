"""Production factory: builds a real ClaudeSDKClient for a SessionDriver."""
from __future__ import annotations
import os


def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=("plan" if mode == "plan" else "default"),
        include_partial_messages=True, resume=resume,
    )
    return ClaudeSDKClient(options=opts)
