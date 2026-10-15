"""Async factories: probe the CLI in a worker, construct SDK clients on the loop."""
from __future__ import annotations
import asyncio
import json
import os

from .account_auth import account_store_io, helper_env
from .claude_bin import cli_path_kwargs as _cli_path_kwargs
from .draft_hold_guard import draft_ready_guard
from .human_hold_guard import human_hold_pre_tool_use
from .merge_hold_guard import merge_hold_guard
from .permissions import to_sdk_permission_mode


def _build_append_string() -> str:
    """This engine's system-prompt append, through the shared ``session_instructions``
    (session tools spec §9), which the Codex driver's fresh ``thread/start`` calls for
    the same purpose.

    Claude is the one engine whose capability is ``durable_tool_surface`` (its declared
    tool surface survives a resume) and whose driver implements self-directed model
    switching (``AgentCapabilities.self_model_switch``), so it is the one engine that
    passes both flags through as True when the surface is on: a Claude session with
    tools mounted is told about the tools and never about the markers they replace.

    ``signalling_tools`` reads ``ClaudeAgentDriver.capabilities.durable_tool_surface``
    rather than trusting ``session_tools_enabled`` alone — the same reasoning that made
    the Codex call read its own capability instead of a literal ``False``: the flag on
    its own says only that SOME overlay is mounted, not that it is the kind a
    signalling-tools paragraph could truthfully describe.
    """
    from server.config import get_settings
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.tools.instruction import session_instructions
    cfg = get_settings()
    return session_instructions(
        session_tools=cfg.session_tools_enabled,
        signalling_tools=(cfg.session_tools_enabled
                          and ClaudeAgentDriver.capabilities.durable_tool_surface),
        self_model_switch=cfg.self_model_switch_enabled,
        child_sessions=cfg.child_sessions_enabled)

# Inline flag-settings payload (--settings layer, highest priority).  Auto-approves any
# project-scoped `.mcp.json` MCP server so the CLI's trust gate (LX1 → "pending" → prompt)
# never fires.  We inject it here rather than via a settings *file* because ViberPit does not
# pass `setting_sources`, so the binary loads no filesystem settings — only this inline layer.
_MCP_TRUST_SETTINGS = json.dumps({"enableAllProjectMcpServers": True})

# Per-line cap on the SDK's stdout reader (``ClaudeAgentOptions.max_buffer_size``). The
# SDK default is 1 MiB, and ONE line over it kills the reader for good while the CLI
# process lives on — the daemon can still write prompts and interrupts, but never
# reads another event, result or permission request. The CLI echoes the whole
# ``originalFile`` in every Edit result on stdout, so the default was tripped by a
# routine edit of a 1,023,418-byte source file (2026-09-16, session 1375c82f: two
# hours of "Waiting…"). 64 MiB clears any source file a session will realistically
# edit; the driver's stream-death recovery (claude_driver._on_stream_ended) is the
# backstop for whatever still exceeds it.
SDK_MAX_BUFFER_BYTES = 64 * 1024 * 1024


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


def session_tmpdir_env() -> dict[str, str]:
    """``CLAUDE_CODE_TMPDIR`` for a coding session, off the shared ``/tmp`` tmpfs.

    The CLI keeps its per-session task dir (Bash output) under ``/tmp/claude-<uid>``; when
    that tmpfs fills, every live session's Bash output fails at once. The directory is shared
    per host (the CLI namespaces its own subdirs) and defaults to
    ``~/.cache/viberpit/claude-tmp``; ``VIBERPIT_SESSION_TMPDIR`` overrides it. An operator-set
    ``CLAUDE_CODE_TMPDIR`` in the daemon env wins. Fails open: an uncreatable directory leaves
    the CLI's own default rather than blocking the session.
    """
    if os.environ.get("CLAUDE_CODE_TMPDIR"):
        return {}
    path = os.environ.get("VIBERPIT_SESSION_TMPDIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "viberpit", "claude-tmp")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return {}
    return {"CLAUDE_CODE_TMPDIR": path}


async def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                     on_pre_compact=None, options_overlay=None, owns_pr=None):
    """Build the real ``ClaudeSDKClient`` for one session.

    *owns_pr*: the session's ``(repo, number, head_ref) -> bool`` ownership check for
    the draft-PR guard (``draft_hold_guard``); the orchestrator binds its
    ``_session_opened_pr`` here.

    *options_overlay*: the assistant's overlay (see ``server/assistant/options.py``)
    replaces individual ``ClaudeAgentOptions`` kwargs wholesale, including
    ``system_prompt``; a coding session's overlay (``server/sessions/tools/``) is
    exactly ``mcp_servers`` and must never carry ``system_prompt`` or
    ``strict_mcp_config`` — the first would erase the marker contracts appended below,
    the second would drop the repo's own ``.mcp.json`` servers.
    """
    ClaudeSDKClient, ClaudeAgentOptions = _import_sdk()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir, **session_tmpdir_env())
    if extra_env:
        env.update(extra_env)
    from claude_agent_sdk import HookMatcher
    # Always on: no mode or allow rule may let a session strip `hold:human` (#3088),
    # merge or arm a `hold:human` PR (#3101), or mark ready a draft it did not open.
    # Without *owns_pr* no draft is its own.
    hooks: dict = {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[
        human_hold_pre_tool_use, draft_ready_guard(owns_pr), merge_hold_guard()])]}
    if on_pre_compact is not None:
        hooks["PreCompact"] = [HookMatcher(hooks=[on_pre_compact])]
    kwargs = dict(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=to_sdk_permission_mode(mode),
        include_partial_messages=True, resume=resume,
        hooks=hooks,
        settings=_MCP_TRUST_SETTINGS,
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _build_append_string(),
        },
        **(await asyncio.to_thread(_cli_path_kwargs)),
    )
    opts = ClaudeAgentOptions(**merge_options_overlay(kwargs, options_overlay))
    return ClaudeSDKClient(options=opts)


async def _deny_all_tools(tool_name, tool_input, ctx=None):
    from claude_agent_sdk import PermissionResultDeny
    return PermissionResultDeny(message="tools are disabled during suggestion generation")


async def real_suggestion_factory(cwd, config_dir, account=None, *, system_prompt=None):
    """One-shot, resume-less client for generating next-prompt suggestions.

    *system_prompt* defaults to the coding-session ``SUGGEST_SYSTEM_PROMPT``; the
    assistant kind binds its own (``server.assistant.suggest_prompt``).

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku but can be overridden (or cleared to the account default) via
    VIBERPIT_SUGGEST_MODEL.

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
    env = await account_store_io(helper_env, config_dir, account)
    model = os.environ.get("VIBERPIT_SUGGEST_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=system_prompt or SUGGEST_SYSTEM_PROMPT, model=model,
        resume=None, **(await asyncio.to_thread(_cli_path_kwargs)),
    )
    return ClaudeSDKClient(options=opts)


async def real_summary_factory(cwd, config_dir, account=None):
    """One-shot, resume-less client for generating a session status summary.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku, overridable via VIBERPIT_SUMMARY_MODEL.

    *account*, when given, also injects CLAUDE_CODE_OAUTH_TOKEN via
    ``account_auth.oauth_env_for`` — the same auth env session launch uses — so an account
    whose browser-OAuth refresh is dead but which holds a live setup-token can still generate
    summaries.

    Env comes from ``account_auth.helper_env`` (see ``real_suggestion_factory``): same auth, plus
    the host-local scratch CLAUDE_CONFIG_DIR that keeps a torn read off the shared store (#1005).
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .summaries import SUMMARY_SYSTEM_PROMPT
    env = await account_store_io(helper_env, config_dir, account)
    model = os.environ.get("VIBERPIT_SUMMARY_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUMMARY_SYSTEM_PROMPT, model=model, resume=None,
        **(await asyncio.to_thread(_cli_path_kwargs)),
    )
    return ClaudeSDKClient(options=opts)
