"""Production factory: builds a real ClaudeSDKClient for a ClaudeAgentDriver."""
from __future__ import annotations
import json
import os

from .account_auth import helper_env
from .claude_bin import cli_path_kwargs as _cli_path_kwargs
from .completion import marker_instructions
from .permissions import to_sdk_permission_mode


def _build_append_string() -> str:
    """This engine's system-prompt append: the shared marker instructions.

    Claude is the one engine whose driver implements self-directed model
    switching (``AgentCapabilities.self_model_switch``), so it is also the one
    engine that passes the flag through; every other engine asks
    ``marker_instructions`` for the completion/no-PR pair alone.
    """
    from server.config import get_settings
    return marker_instructions(
        self_model_switch=get_settings().self_model_switch_enabled
    )

# Inline flag-settings payload (--settings layer, highest priority).  Auto-approves any
# project-scoped `.mcp.json` MCP server so the CLI's trust gate (LX1 → "pending" → prompt)
# never fires.  We inject it here rather than via a settings *file* because ccswitch does not
# pass `setting_sources`, so the binary loads no filesystem settings — only this inline layer.
_MCP_TRUST_SETTINGS = json.dumps({"enableAllProjectMcpServers": True})


def _import_sdk():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    return ClaudeSDKClient, ClaudeAgentOptions


# Options the CLI-resolution layer owns outright: an overlay may never replace them.
# Losing `cli_path` means the SDK silently spawns its own vendored, older `claude`
# (2.1.183 vs the system 2.1.259) — a failure mode this project has hit twice, and one
# that shows up as "unsupported model" 400s rather than as anything pointing at the path.
_FACTORY_OWNED_KWARGS = ("cli_path", "path_to_claude_code_executable")


def merge_options_overlay(kwargs: dict, overlay: dict | None) -> dict:
    """Merge *overlay* over the factory's *kwargs*, keeping the factory-owned keys.

    Returns a new dict; *kwargs* is not mutated.
    """
    if not overlay:
        return dict(kwargs)
    merged = {**kwargs, **overlay}
    for key in _FACTORY_OWNED_KWARGS:
        if key in kwargs:
            merged[key] = kwargs[key]
    return merged


def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                     on_pre_compact=None, options_overlay=None):
    """Build the real ``ClaudeSDKClient`` for one session.

    *options_overlay* (assistant sessions only — see ``server/assistant/options.py``)
    replaces individual ``ClaudeAgentOptions`` kwargs wholesale, including
    ``system_prompt``: the ccswitch marker instructions appended below are coding-session
    contracts (completion marker, PR marker, self-directed model request) and mean
    nothing to the assistant.
    """
    ClaudeSDKClient, ClaudeAgentOptions = _import_sdk()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    if extra_env:
        env.update(extra_env)
    hooks = None
    if on_pre_compact is not None:
        from claude_agent_sdk import HookMatcher
        hooks = {"PreCompact": [HookMatcher(hooks=[on_pre_compact])]}
    kwargs = dict(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=to_sdk_permission_mode(mode),
        include_partial_messages=True, resume=resume,
        hooks=hooks,
        settings=_MCP_TRUST_SETTINGS,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _build_append_string(),
        },
        **_cli_path_kwargs(),
    )
    opts = ClaudeAgentOptions(**merge_options_overlay(kwargs, options_overlay))
    return ClaudeSDKClient(options=opts)


async def _deny_all_tools(tool_name, tool_input, ctx=None):
    from claude_agent_sdk import PermissionResultDeny
    return PermissionResultDeny(message="tools are disabled during suggestion generation")


def real_suggestion_factory(cwd, config_dir, account=None):
    """One-shot, resume-less client for generating next-prompt suggestions.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku but can be overridden (or cleared to the account default) via
    CCSWITCH_SUGGEST_MODEL.

    *account*, when given, also injects CLAUDE_CODE_OAUTH_TOKEN via
    ``account_auth.oauth_env_for`` — the same auth env session launch uses — so an account
    whose browser-OAuth refresh is dead but which holds a live setup-token can still generate
    suggestions.

    Env comes from ``account_auth.helper_env``, the shared one-shot-helper builder: it adds the
    same auth AND points CLAUDE_CONFIG_DIR at a host-local scratch copy, so this spawn cannot
    reset the fleet's shared config after a torn read (#1005).
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .suggestions import SUGGEST_SYSTEM_PROMPT
    env = helper_env(config_dir, account)
    model = os.environ.get("CCSWITCH_SUGGEST_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUGGEST_SYSTEM_PROMPT, model=model, resume=None,
        **_cli_path_kwargs(),
    )
    return ClaudeSDKClient(options=opts)


def real_summary_factory(cwd, config_dir, account=None):
    """One-shot, resume-less client for generating a session status summary.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku, overridable via CCSWITCH_SUMMARY_MODEL.

    *account*, when given, also injects CLAUDE_CODE_OAUTH_TOKEN via
    ``account_auth.oauth_env_for`` — the same auth env session launch uses — so an account
    whose browser-OAuth refresh is dead but which holds a live setup-token can still generate
    summaries.

    Env comes from ``account_auth.helper_env`` (see ``real_suggestion_factory``): same auth, plus
    the host-local scratch CLAUDE_CONFIG_DIR that keeps a torn read off the shared store (#1005).
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .summaries import SUMMARY_SYSTEM_PROMPT
    env = helper_env(config_dir, account)
    model = os.environ.get("CCSWITCH_SUMMARY_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUMMARY_SYSTEM_PROMPT, model=model, resume=None,
        **_cli_path_kwargs(),
    )
    return ClaudeSDKClient(options=opts)
