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


def _build_append_string(platform_guide: str | None = None) -> str:
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
        child_sessions=cfg.child_sessions_enabled,
        platform_guide=platform_guide)

# Inline flag-settings payload (--settings layer, highest priority).  Auto-approves any
# project-scoped `.mcp.json` MCP server so the CLI's trust gate (LX1 → "pending" → prompt)
# never fires.  ViberPit passes no `setting_sources`, so the binary loads EVERY filesystem
# source — user, project and local settings and CLAUDE.md memory (D-13 verification spike §1);
# this inline layer sits above the user, project and local layers, so none of them overrides it.
_MCP_TRUST_SETTINGS = {"enableAllProjectMcpServers": True}


def spawn_settings(flag_settings: dict | None = None) -> str:
    """The ``--settings`` JSON for one spawn: the MCP trust above plus the session's own
    flag-layer settings (D-14: ``fastMode``, ``effortLevel``). Spawn-time only — it is
    never written to the shared account ``settings.json``. The session's settings win
    over the trust default: a sandboxed spawn (D-23,
    ``sandbox.spawn_settings_overlay``) turns project-MCP auto-trust OFF."""
    return json.dumps({**_MCP_TRUST_SETTINGS, **(flag_settings or {})})

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


def session_tmpdir_path() -> str:
    """The Claude temp root a session's CLI runs with: the daemon's own
    ``CLAUDE_CODE_TMPDIR`` if set, else ``VIBERPIT_SESSION_TMPDIR``, else
    ``~/.cache/viberpit/claude-tmp``. ``session_tmpdir_env`` sets it; the sandbox check
    (D-23) allows writes under it."""
    return (os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("VIBERPIT_SESSION_TMPDIR")
            or os.path.join(os.path.expanduser("~"), ".cache", "viberpit", "claude-tmp"))


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
    path = session_tmpdir_path()
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return {}
    return {"CLAUDE_CODE_TMPDIR": path}


async def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                     on_pre_compact=None, options_overlay=None, owns_pr=None,
                     ready_grants=None, fork_session=False, extra_args=None,
                     enable_file_checkpointing=False, platform_guide=None,
                     flag_settings=None, stderr=None, sandbox_confine=None,
                     sandbox_post_write=None):
    """Build the real ``ClaudeSDKClient`` for one session.

    *owns_pr*: the session's ``(repo, number, head_ref) -> bool`` ownership check for
    the draft-PR guard (``draft_hold_guard``); the orchestrator binds its
    ``_session_opened_pr`` here. *ready_grants*: that guard's view of the drafts the
    user approved for this session (``DraftReadyGrants.for_session``).

    *fork_session* / *extra_args* (D-8 Fork from here): with *resume* set to the
    SOURCE session's id, ``fork_session=True`` and ``extra_args={"resume-session-at":
    <uuid>}`` start a NEW engine session holding the source's history through that
    transcript entry (``--resume <sid> --fork-session --resume-session-at <uuid>``).

    *enable_file_checkpointing* (D-9 Rewind): the SDK sets
    ``CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING`` in the CLI's env, so turns run by this
    process get file checkpoints ``rewind_files`` can restore. Spawn-time only — the
    orchestrator passes it on every coding spawn while rewind is on.

    *flag_settings* (D-14): the session's stored flag-layer settings (``fastMode``,
    ``effortLevel``), merged into the ``--settings`` JSON so a resume or respawn starts
    with them; live changes go through ``ClaudeAgentDriver.apply_flag_settings``.

    *sandbox_confine* (D-23): the PreToolUse callback confining non-Bash tools for a
    sandboxed spawn (``sandbox.confinement_hook``). Registered with no matcher, so it
    sees every tool call (2.1.319: a matcher-less hook matches all, @295677744); it
    binds in every permission mode (see ``sandbox``).

    *sandbox_post_write* (D-23): the PostToolUse callback that holds the session when a
    file tool's target resolved outside the worktree after the write
    (``sandbox.post_write_hook``).

    *stderr* (D-23): a per-line callback for the CLI's stderr, passed only for a
    sandboxed spawn, so the driver can read the CLI's own sandbox refusal ("❌ Sandbox
    Error", "sandbox required but unavailable") when the spawn fails. The sandbox block
    itself rides *flag_settings*.

    *platform_guide*: the session's per-platform guide text (``platform_guide.py``),
    appended to the system prompt; ``None`` for a Linux session.

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
    # merge or arm a `hold:human` PR (#3101), or mark ready a draft it did not open
    # unless the user approved that PR. Without *owns_pr* no draft is its own.
    hooks: dict = {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[
        human_hold_pre_tool_use, draft_ready_guard(owns_pr, grants=ready_grants),
        merge_hold_guard()])]}
    if sandbox_confine is not None:
        hooks["PreToolUse"].append(HookMatcher(hooks=[sandbox_confine]))
    if sandbox_post_write is not None:
        hooks["PostToolUse"] = [HookMatcher(hooks=[sandbox_post_write])]
    if on_pre_compact is not None:
        hooks["PreCompact"] = [HookMatcher(hooks=[on_pre_compact])]
    kwargs = dict(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=to_sdk_permission_mode(mode),
        include_partial_messages=True, resume=resume,
        fork_session=bool(fork_session), extra_args=dict(extra_args or {}),
        enable_file_checkpointing=bool(enable_file_checkpointing),
        hooks=hooks,
        settings=spawn_settings(flag_settings),
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _build_append_string(platform_guide),
        },
        **(await asyncio.to_thread(_cli_path_kwargs)),
    )
    if stderr is not None:
        kwargs["stderr"] = stderr
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
