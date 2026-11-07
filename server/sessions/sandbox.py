"""Opt-in, fail-closed sandbox for Claude sessions (D-23).

The source of truth for every fact here is the spike,
``docs/superpowers/specs/2026-11-06-d23-sandbox-spike.md`` (CLI 2.1.319 / 2.1.293): the
settings block (§2.1), the host preflight (§2.3), the after-spawn check (§2.4) and the
failure behaviour (§2.5). This module is the one vocabulary every hop agrees on:

* **Settings.** :func:`settings_block` is the exact ``sandbox`` object merged into the
  spawn's ``--settings`` JSON. It is a module constant, always sent whole (merge
  semantics are unverified, risk 11), and none of it is configurable by a session:
  ``allowUnsandboxedCommands: false``, ``network.strictAllowlist: true`` and
  ``network.allowAllUnixSockets: false`` are fixed here.
* **Session env.** :func:`session_env` gives a sandboxed session ``GH_CONFIG_DIR`` (a
  file-backed gh token, so gh and git's credential helper need no D-Bus socket) and
  ``GH_NO_UPDATE_NOTIFIER=1``.
* **Host preflight.** :func:`run_preflight` (argv-only, each subprocess bounded), cached
  per process by :func:`preflight` — one daemon is one host. The first failure is the
  reason the toggle shows.
* **After-spawn check.** :func:`check_dialog` reads the CLI's ``get_sandbox_dialog``
  reply. The request is ``@internal``: absent or erroring means NOT verified, and the
  driver holds the session (``ClaudeAgentDriver._enter_sandboxed``).
* **Refusals.** :class:`SandboxError` carries one :data:`SANDBOX_CODES` code across the
  daemon's RPC envelope; :func:`http_status` and :data:`MESSAGES` are the edge's answer.

Never retried unsandboxed: a session whose sandbox could not start or be verified is
parked with :data:`SANDBOX_PAUSE_REASON` (terminal — no automatic loop rebuilds it), and
only a person's Retry or "Turn off sandbox and continue" moves it on.

Kill switch ``VIBERPIT_SANDBOX_ENABLED`` (constitution I): off, no engine advertises
``sandbox``, no spawn carries the block or the env, and the toggle is refused.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

log = logging.getLogger(__name__)

#: The ``--settings`` key the block lives under.
SANDBOX_KEY = "sandbox"

#: Network allowlist (D22d amendment 5): GitHub only, denied outright otherwise.
ALLOWED_DOMAINS: tuple[str, ...] = ("github.com", "*.github.com", "*.githubusercontent.com")

#: The oldest CLI the spike verified (rocky, 2.1.293); dobby ran 2.1.319.
MIN_CLI_VERSION: tuple[int, int, int] = (2, 1, 293)
#: The CLI's own dependency floor for bubblewrap (spike §1.2).
MIN_BWRAP_VERSION: tuple[int, int, int] = (0, 5, 0)
#: Each preflight subprocess's deadline; the namespace probe's is the spike's 5 s.
PROBE_TIMEOUT_S = 5.0
#: ``get_sandbox_dialog``'s reply deadline. A local read of the CLI's own state.
SANDBOX_CONTROL_TIMEOUT_S = 15.0

#: The CLI's startup refusal (``startup_failure_reason``) and its text forms.
SANDBOX_REQUIRED_UNAVAILABLE = "sandbox_required_unavailable"
_STARTUP_MARKERS: tuple[str, ...] = (
    "sandbox required but unavailable",
    SANDBOX_REQUIRED_UNAVAILABLE,
    "sandbox error",          # "❌ Sandbox Error": deps present but initialize() threw
)
#: The dialog warning that means unix sockets are NOT blocked (spike §1.2).
_SECCOMP_MISSING = "seccomp not available"

#: The one pause reason a sandbox hold uses (``orchestrator.PAUSE_REASONS``): terminal,
#: so no automatic loop retries it; Retry / Turn off are a person's.
SANDBOX_PAUSE_REASON = "sandbox_failed"
#: A file tool's target resolved outside the worktree AFTER the write (fix round 3):
#: terminal like ``sandbox_failed``, and a held session either way.
SANDBOX_ESCAPE_REASON = "sandbox_escape_suspected"
SANDBOX_HOLD_REASONS: frozenset[str] = frozenset({SANDBOX_PAUSE_REASON,
                                                   SANDBOX_ESCAPE_REASON})

# The session's sandbox state, as stored (``sessions.sandbox_state``) and served.
STATE_OFF = "off"
STATE_PENDING = "pending"     # asked for; this CLI has not been verified yet
STATE_ACTIVE = "active"
STATE_FAILED = "failed"

# ── refusal codes ────────────────────────────────────────────────────────────
SANDBOX_DISABLED = "SANDBOX_DISABLED"
SANDBOX_UNSUPPORTED = "SANDBOX_UNSUPPORTED"
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
SANDBOX_SESSION_BUSY = "SANDBOX_SESSION_BUSY"
SANDBOX_SESSION_ENDED = "SANDBOX_SESSION_ENDED"
SANDBOX_START_FAILED = "SANDBOX_START_FAILED"
SANDBOX_UNVERIFIED = "SANDBOX_UNVERIFIED"
SANDBOX_HELD = "SANDBOX_HELD"

#: Every code a daemon may raise; a remote edge rebuilds only these.
SANDBOX_CODES: frozenset[str] = frozenset({
    SANDBOX_DISABLED, SANDBOX_UNSUPPORTED, SANDBOX_UNAVAILABLE, SANDBOX_SESSION_BUSY,
    SANDBOX_SESSION_ENDED, SANDBOX_START_FAILED, SANDBOX_UNVERIFIED, SANDBOX_HELD,
})

MESSAGES: dict[str, str] = {
    SANDBOX_DISABLED: "The sandbox is turned off on this server (VIBERPIT_SANDBOX_ENABLED), "
                      "so a session that asked for it is not run. Turn the sandbox off "
                      "for this session to continue without it.",
    SANDBOX_UNSUPPORTED: "This session's agent cannot run in the sandbox.",
    SANDBOX_UNAVAILABLE: "This host cannot run the sandbox.",
    SANDBOX_SESSION_BUSY: "The session is busy. Wait for the turn to finish (or Interrupt "
                          "it), answer any open card, and let background tasks end first.",
    SANDBOX_SESSION_ENDED: "The session has ended.",
    SANDBOX_START_FAILED: "The sandbox could not start, so the session was not run.",
    SANDBOX_UNVERIFIED: "The sandbox could not be verified, so the session was stopped.",
    SANDBOX_HELD: "The session is held because its sandbox failed. Retry, or turn the "
                  "sandbox off to continue without it.",
}


def http_status(code: str) -> int:
    """Every refusal is a state that refuses it: 409."""
    return 409


class SandboxError(Exception):
    """A sandbox refusal or failure. ``error_data`` rides the daemon's RPC envelope
    (``code`` plus ``reason``), so a remote edge rebuilds it, and marks it an expected
    refusal the daemon logs at INFO. ``reason`` is the specific cause (the CLI's own
    refusal text, the failed dialog condition, the preflight's first failure)."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        if code not in SANDBOX_CODES:
            raise ValueError(f"unknown sandbox code {code!r}")
        super().__init__(reason or MESSAGES[code])
        self.code = code
        self.reason = reason
        self.error_data: dict[str, Any] = {"code": code, "reason": reason}


def is_sandbox_error(exc: BaseException) -> bool:
    """A :class:`SandboxError`, here or rebuilt from a daemon envelope's ``err_type``."""
    if isinstance(exc, SandboxError):
        return True
    return getattr(exc, "err_type", None) == "SandboxError"


# ── kill switch and capability ───────────────────────────────────────────────


def sandbox_enabled() -> bool:
    """The ``VIBERPIT_SANDBOX_ENABLED`` kill switch (D-23, default on)."""
    from server.config import get_settings
    return bool(get_settings().sandbox_enabled)


def sandbox_supported(caps: Any) -> bool:
    """THE gate, shaped like ``engine.flag_settings_supported``: the engine declares
    ``sandbox`` and the kill switch is on. ``None`` (unknown engine) never does."""
    return caps is not None and bool(getattr(caps, "sandbox", False)) and sandbox_enabled()


def takes_sandbox(record: Any, caps: Any) -> bool:
    """Is this session the kind that can carry a sandbox? A coding session (the Assistant
    drawer runs no shell) on an engine that declares ``sandbox``. Deliberately blind to
    the kill switch: a session whose recorded setting is on stays on — and is refused,
    never run plain — while the switch is off (:func:`spawn_requested`)."""
    from server.sessions.types import is_assistant_kind
    return (record is not None and not is_assistant_kind(getattr(record, "kind", None))
            and caps is not None and bool(getattr(caps, "sandbox", False)))


def offers_sandbox(record: Any, caps: Any) -> bool:
    """May the UI offer this session the toggle (and may a new session default to on)?
    A session that can take one, with the kill switch on."""
    return takes_sandbox(record, caps) and sandbox_enabled()


def spawn_requested(record: Any, caps: Any) -> bool:
    """Does a spawn of this session carry the sandbox? Its recorded setting is on.

    Fail closed: an engine that cannot take one is REFUSED (``SANDBOX_UNSUPPORTED``,
    raised here — its driver would ignore the flag and run plain), and the kill switch
    being off does not turn this False: the driver refuses the start
    (``SANDBOX_DISABLED``) and the session is held. A legacy row (``sandbox`` NULL:
    spawned before D-23) reads as off, so flipping the default on never holds every
    existing session at the next restart."""
    from server.sessions.types import is_assistant_kind
    if record is None or getattr(record, "sandbox", None) is not True:
        return False
    if is_assistant_kind(getattr(record, "kind", None)):
        return False
    if not takes_sandbox(record, caps):
        raise SandboxError(SANDBOX_UNSUPPORTED,
                           "this session's engine cannot run in the sandbox")
    return True


# ── settings and env ─────────────────────────────────────────────────────────

# Schema evidence (CLI 2.1.319): sandbox `GLr` @282541783, network `ph` @282520636,
# filesystem `gh` @282524116 (spike §1.1). Every key below is one the binary knows; the
# loosening ones are pinned to their safe value so no lower layer can turn them on.
_SETTINGS_BLOCK: dict[str, Any] = {
    "enabled": True,
    # Fail closed at spawn: the CLI refuses to start instead of running Bash bare.
    "failIfUnavailable": True,
    # Keep ViberPit's permission cards: Bash takes the normal can_use_tool path.
    "autoAllowBashIfSandboxed": False,
    # No `dangerouslyDisableSandbox` escape, which full-auto would approve silently.
    "allowUnsandboxedCommands": False,
    "excludedCommands": [],
    "network": {
        "allowedDomains": list(ALLOWED_DOMAINS),
        # Deny unlisted hosts outright: no SandboxNetworkAccess asks reach the registry.
        "strictAllowlist": True,
        # Linux has no per-path socket filter; "all" would open docker and D-Bus.
        "allowAllUnixSockets": False,
    },
    # Filesystem isolation stays on (`filesystem.disabled`, honored from --settings).
    "filesystem": {"disabled": False},
    # Linux: would skip the fresh /proc — "Reduces security" (GLr).
    "enableWeakerNestedSandbox": False,
    # "Removes code-execution isolation" (GLr).
    "allowNestedUserNamespaces": False,
    # No command's violations are hidden.
    "ignoreViolations": {},
}


def protected_git_paths(cwd: str | None) -> list[str]:
    """``<common>/hooks`` and ``<common>/config`` of the worktree's common git dir: the
    CLI write-denies them itself (spike §1.6, ``So()``), and ViberPit names them in the
    block's ``filesystem.denyWrite`` too, so the after-spawn check can see them applied
    (the dialog reports only the settings' lists, not the runtime's defaults)."""
    common = git_common_dir(cwd)
    return [os.path.join(common, "hooks"), os.path.join(common, "config")] if common else []


def settings_block(cwd: str | None = None, config_dir: str | None = None,
                   tmpdir: str | None = None) -> dict[str, Any]:
    """``{"sandbox": {...}}`` — the full block for one spawn's ``--settings`` JSON (spike
    §2.1, plus the pins). A fresh deep copy every call: nothing a caller does to it
    reaches the next. With *cwd*, ``filesystem.denyWrite`` names that worktree's git
    ``hooks``/``config``; with *config_dir*, ``filesystem.denyRead`` / ``allowRead``
    hide the secrets a sandboxed shell could otherwise read (:func:`read_policy`;
    2.1.319 filesystem schema ``gh`` @282524116, ``denyRead`` @282524433)."""
    block = copy.deepcopy(_SETTINGS_BLOCK)
    deny = protected_git_paths(cwd)
    if deny:
        block["filesystem"]["denyWrite"] = deny
    if config_dir is not None:
        deny_read, allow_read = read_policy(config_dir, cwd, tmpdir)
        block["filesystem"]["denyRead"] = deny_read
        block["filesystem"]["allowRead"] = allow_read
    return {SANDBOX_KEY: block}


def spawn_settings_overlay(cwd: str | None = None, config_dir: str | None = None,
                           tmpdir: str | None = None) -> dict[str, Any]:
    """Everything a SANDBOXED spawn adds to its ``--settings`` JSON (flag layer):

    * the sandbox block (:func:`settings_block`);
    * ``disableAllHooks: true`` — settings-file hooks (a repo's ``.claude/settings.json``,
      local, user, plugin) run as host processes outside bwrap. In the flag layer it
      leaves only policy hooks (2.1.319: the hooks resolver ``r()`` @286836077
      returns ``policySettings.hooks`` when the flag source disables hooks; ``y()`` in
      ``l6`` @286862778 drops plugin hooks via ``y_()``) and keeps the SDK host's own
      callback hooks (registered ``origin: "sdkHost"`` hooks have no ``pluginRoot`` and
      pass ``y()``'s filter). CLAUDE.md memory is a separate loader and is untouched;
      ``setting_sources`` is NOT used, since it would also drop CLAUDE.md;
    * ``enableAllProjectMcpServers: false`` — a repo's ``.mcp.json`` servers would start
      as host processes outside bwrap; the unsandboxed default auto-trusts them.

    Trade-off (acknowledged, fix round 3): ``disableAllHooks`` also stops the operator's
    USER hooks in sandboxed sessions; the only one in use is the WebSearch transformer,
    and WebSearch is denied there anyway.
    """
    return {**settings_block(cwd, config_dir, tmpdir), "disableAllHooks": True,
            "enableAllProjectMcpServers": False}


_OAUTH_TOKEN_RE = re.compile(r"^\s*oauth_token:\s*\S+", re.MULTILINE)


def gh_file_backed(config_dir: str | None) -> bool:
    """Does *config_dir*'s ``hosts.yml`` hold a token (gh's insecure-storage form)? A
    keyring-only config keeps ``hosts.yml`` without one and needs the D-Bus socket the
    sandbox blocks. A read, not ``gh auth status`` (which calls the API)."""
    if not config_dir:
        return False
    try:
        with open(os.path.join(os.path.expanduser(config_dir), "hosts.yml"),
                  encoding="utf-8") as fh:
            return bool(_OAUTH_TOKEN_RE.search(fh.read(256 * 1024)))
    except OSError:
        return False


def _viberpit_gh_config_dir() -> str | None:
    raw = os.environ.get("VIBERPIT_GH_CONFIG_DIR")
    return os.path.expanduser(raw) if raw else None


def _ambient_gh_config_dir() -> str:
    """The config dir a session's gh reads with no ``GH_CONFIG_DIR`` of ours: the
    daemon's own ``GH_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/gh``, else ``~/.config/gh``."""
    if os.environ.get("GH_CONFIG_DIR"):
        return os.path.expanduser(os.environ["GH_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"),
                                                             ".config")
    return os.path.join(base, "gh")


def session_env() -> dict[str, str]:
    """The env a SANDBOXED session's CLI gets on top of the usual one.

    ``GH_CONFIG_DIR`` is ``VIBERPIT_GH_CONFIG_DIR`` when that dir holds a file-backed
    token: gh reads only ``GH_CONFIG_DIR`` (spike §1.5 probe E), and with it the
    credential helper needs no D-Bus socket. Set for sandboxed sessions ONLY: that dir
    is the dedicated ViberPit identity (``gitrepos._gh_env``), and pointing every
    unsandboxed session at it would change whose token their gh uses and where a
    session's own ``gh auth login`` writes. ``GH_NO_UPDATE_NOTIFIER=1`` because gh's
    update check writes outside the worktree (risk 4)."""
    env = {"GH_NO_UPDATE_NOTIFIER": "1"}
    dedicated = _viberpit_gh_config_dir()
    if dedicated and gh_file_backed(dedicated):
        env["GH_CONFIG_DIR"] = dedicated
    return env


# ── after-spawn check ────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class DialogVerdict:
    """The §2.4 check over one ``get_sandbox_dialog`` reply. ``expected`` marks a
    host condition (missing deps, unavailable) — an INFO refusal — as opposed to a
    reply that contradicts the settings ViberPit sent (WARNING)."""
    ok: bool
    reason: str | None = None
    expected: bool = False


def git_common_dir(cwd: str | None) -> str | None:
    """The worktree's common git dir (``<clone>/.git``; spike §1.6, ``fwr()``
    @288687776), or None."""
    if not cwd:
        return None
    dot = os.path.join(cwd, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        with open(dot, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(cwd, line[len("gitdir:"):].strip()))
    parent = os.path.dirname(gitdir)
    return os.path.dirname(parent) if os.path.basename(parent) == "worktrees" else gitdir


def _str_list(value: Any) -> list[str] | None:
    return [str(v) for v in value] if isinstance(value, list) else None


def check_dialog(resp: Any, *, cwd: str | None = None, tmpdir: str | None = None,
                 config_dir: str | None = None) -> DialogVerdict:
    """Require every condition of spike §2.4, and that the restrictions the CLI says it
    applied are exactly ViberPit's block. The reply's restriction lists are "the
    settings' lists, not the runtime's built-in defaults" (2.1.319 schema @284671000),
    and every one used here is emitted by both verified CLIs (2.1.293 and 2.1.319 carry
    ``excluded_commands`` / ``fs_allow_write`` / ``fs_deny_write`` /
    ``network_allowed_domains``), so an ABSENT list fails like a wrong one:

    * ``excluded_commands == []``;
    * ``restrictions.network_allowed_domains`` within :data:`ALLOWED_DOMAINS`;
    * ``restrictions.fs_allow_write == []`` — it lists only EXTRA writable paths
      (``filesystem.allowWrite``), so anything there was added by some settings layer;
    * ``restrictions.fs_deny_write`` includes the worktree's ``<common>/hooks`` and
      ``<common>/config`` (sent in the block's ``filesystem.denyWrite``).
    * with *config_dir*: ``restrictions.fs_deny_read`` is exactly the :func:`read_policy`
      denial and ``fs_allow_read`` is exactly its re-allows (sent in the block).

    *tmpdir* is the session's Claude temp root, one of the read re-allows."""
    if not isinstance(resp, Mapping):
        return DialogVerdict(False, "get_sandbox_dialog returned no sandbox state")
    deps = resp.get("dependencies")
    errors = deps.get("errors") if isinstance(deps, Mapping) else None
    warnings = deps.get("warnings") if isinstance(deps, Mapping) else None
    unavailable = resp.get("unavailable_reason")
    if unavailable:
        return DialogVerdict(False, f"sandbox unavailable: {unavailable}", expected=True)
    if errors:
        return DialogVerdict(False, "sandbox dependencies missing: "
                             + "; ".join(str(e) for e in errors), expected=True)
    if resp.get("enabled_in_settings") is not True:
        return DialogVerdict(False, "the sandbox settings were not applied")
    if resp.get("enabled") is not True:
        return DialogVerdict(False, "the sandbox did not start")
    if not isinstance(errors, list) or not isinstance(warnings, list):
        return DialogVerdict(False, "get_sandbox_dialog did not report its dependencies")
    if any(_SECCOMP_MISSING in str(w).lower() for w in warnings):
        return DialogVerdict(False, "seccomp is not available, so unix sockets would not "
                             "be blocked", expected=True)
    if resp.get("mode") != "regular":
        return DialogVerdict(False, f"sandbox mode is {resp.get('mode')!r}, not 'regular'")
    if resp.get("unsandboxed_fallback") is not False:
        return DialogVerdict(False, "unsandboxed fallback is allowed")
    restrictions = resp.get("restrictions")
    if not (isinstance(restrictions, Mapping)
            and restrictions.get("allow_all_unix_sockets") is False):
        return DialogVerdict(False, "unix sockets are not blocked")
    excluded = _str_list(resp.get("excluded_commands"))
    if excluded is None:
        return DialogVerdict(False, "get_sandbox_dialog did not report excluded commands")
    if excluded:
        return DialogVerdict(False, "commands are excluded from the sandbox: "
                             + ", ".join(excluded))
    domains = _str_list(restrictions.get("network_allowed_domains"))
    if domains is None:
        return DialogVerdict(False, "get_sandbox_dialog did not report the network "
                             "allowlist")
    extra = [d for d in domains if d not in ALLOWED_DOMAINS]
    if extra:
        return DialogVerdict(False, "the network allowlist is wider than GitHub: "
                             + ", ".join(extra))
    writes = _str_list(restrictions.get("fs_allow_write"))
    if writes is None:
        return DialogVerdict(False, "get_sandbox_dialog did not report the writable paths")
    if writes:
        return DialogVerdict(False, "extra writable paths are allowed: " + ", ".join(writes))
    denied = _str_list(restrictions.get("fs_deny_write"))
    if denied is None:
        return DialogVerdict(False, "get_sandbox_dialog did not report the write denials")
    missing = [p for p in protected_git_paths(cwd) if p not in denied]
    if missing:
        return DialogVerdict(False, "git hooks/config are not write-denied: "
                             + ", ".join(missing))
    if config_dir is not None:
        want_deny, want_allow = read_policy(config_dir, cwd, tmpdir)
        deny_read = _str_list(restrictions.get("fs_deny_read"))
        allow_read = _str_list(restrictions.get("fs_allow_read"))
        if deny_read is None or allow_read is None:
            return DialogVerdict(False, "get_sandbox_dialog did not report the read rules")
        unread = [p for p in want_deny if p not in deny_read]
        if unread:
            return DialogVerdict(False, "secrets are not read-denied: " + ", ".join(unread))
        extra_deny = sorted(set(deny_read) - set(want_deny))
        if extra_deny:
            return DialogVerdict(False, "the read denials differ from the block: "
                                 + ", ".join(extra_deny))
        if sorted(set(allow_read)) != sorted(set(want_allow)):
            return DialogVerdict(False, "the read re-allows differ from the block: "
                                 + ", ".join(sorted(set(allow_read) ^ set(want_allow))))
    return DialogVerdict(True)


# ── tools (D22d: the sandbox contains a full-auto session) ───────────────────
# The CLI sandbox wraps Bash only; every other tool runs in the CLI host process. So a
# sandboxed session runs a DENY-BY-DEFAULT allowlist (``confine_tool``), enforced by an
# SDK PreToolUse callback hook — it binds in EVERY permission mode, bypassPermissions
# included (2.1.319: PreToolUse hooks run before the permission pipeline, and ``Nds``
# @296090015 returns a hook "deny" before it calls canUseTool or reads the mode) — and
# again in the driver's can_use_tool. Sub-agents run their tools through the same
# executor (``e8t``), whose only hook gate is ``q0`` @295954344 (internal always-allowed
# tools, a ``bareFork`` — set only for the resume cache-warm fork @322680079 — or
# ``settingsHooksOff``, never set to true in this binary), so the hook sees their calls.

#: Tools a sandboxed session may run with no further constraint: the kernel-sandboxed
#: shell and its background-output controls, conversation tools, sub-agents.
PLAIN_TOOLS: frozenset[str] = frozenset({
    "Bash", "BashOutput", "KillShell", "KillBash", "TaskOutput", "TaskStop",
    "Task", "Agent", "TodoWrite", "ExitPlanMode", "AskUserQuestion",
})
#: Read-only file tools, confined to the worktree, its common git dir and the Claude temp
#: root, and refused for secrets.
READ_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})

_GLOB_CHARS = frozenset("*?[{")


class _Refused(Exception):
    pass


def _lexical_abs(raw: str, cwd: str) -> str:
    """*raw* as an absolute, lexically normalised path, relative to *cwd* and re-based
    onto ``realpath(cwd)`` when it names a place under *cwd* (the model writes the path
    the CLI gave it, which may run through a symlinked parent). Refuses a NUL, a leading
    ``~`` (the CLI expands it to $HOME) and any ``..`` component: the CLI may normalise
    ``..`` before the kernel resolves symlinks, so the two readings can disagree."""
    if not raw or "\x00" in raw or raw.startswith("~") or ".." in raw.split(os.sep):
        raise _Refused(f"{raw!r} is not an acceptable path")
    cwd_abs = os.path.abspath(cwd)
    path = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(cwd_abs, raw))
    if _within(path, cwd_abs):
        path = os.path.normpath(os.path.join(os.path.realpath(cwd_abs),
                                             os.path.relpath(path, cwd_abs)))
    return path


def _within(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def _no_symlink_below(base: str, path: str) -> None:
    """Refuse when any component of *path* below *base* (both real, lexical) is a symlink,
    or the target is a multiply-linked file: a racing sandboxed process could swap a link
    in after the check (TOCTOU), and a hard link names a file outside the tree."""
    rel = os.path.relpath(path, base)
    cur = base
    for part in ([] if rel == "." else rel.split(os.sep)):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise _Refused(f"{cur!r} is a symlink")
    try:
        st = os.lstat(path)
    except OSError:
        return                       # a file the tool will create
    import stat as _stat
    if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise _Refused(f"{path!r} has other hard links")


def _resolve_in_worktree(raw: str, cwd: str) -> tuple[str, str]:
    """``(absolute path, worktree-relative path)`` for a target inside the worktree, with
    no symlink component and no extra hard link; raises ``_Refused`` otherwise."""
    base = os.path.realpath(cwd)
    path = _lexical_abs(raw, cwd)
    if not _within(path, base) or path == base:
        raise _Refused(f"{raw!r} is outside this session's worktree")
    _no_symlink_below(base, path)
    if os.path.realpath(path) != path:
        raise _Refused(f"{raw!r} does not resolve to itself")
    return path, os.path.relpath(path, base).replace(os.sep, "/")


def _read_target_ok(raw: str, cwd: str, tmpdir: str | None) -> None:
    """A read target must be inside the worktree (not a protected path), the clone's
    common git dir (read-only) or the Claude temp root; symlink-safe like writes."""
    from server.permission_rules.patterns import is_protected_path
    base = os.path.realpath(cwd)
    path = _lexical_abs(raw, cwd)
    if _within(path, base):
        if path == base:
            return
        _no_symlink_below(base, path)
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if is_protected_path(rel):
            raise _Refused(f"{raw!r} is protected (secrets, git internals and tool "
                           "configuration are not readable from a sandboxed session)")
        return
    for extra in (git_common_dir(base), tmpdir):
        if not extra:
            continue
        root = os.path.realpath(extra)
        if _within(path, root):
            _no_symlink_below(root, path)
            return
    raise _Refused(f"{raw!r} is outside this session's worktree")


def _read_paths(tool: str, inp: Mapping[str, Any]) -> list[str]:
    """Every path a read tool would touch; ``[]`` = the worktree itself."""
    from server.permission_rules.patterns import tool_path_arg
    if tool in ("Read", "NotebookRead"):
        key = "notebook_path" if tool == "NotebookRead" else "file_path"
        raw = (tool_path_arg("NotebookEdit", inp) if tool == "NotebookRead"
               else tool_path_arg("Read", inp))
        if raw is None:
            raise _Refused(f"{tool} needs one {key}")
        return [raw]
    out: list[str] = []
    path = inp.get("path")
    if path is not None:
        if not isinstance(path, str) or not path:
            raise _Refused(f"{tool} has an unreadable path")
        out.append(path)
    if tool == "Glob":
        pattern = inp.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise _Refused("Glob needs a pattern")
        if ".." in pattern.split("/"):
            raise _Refused("a Glob pattern may not climb with ..")
        if os.path.isabs(pattern):
            # The fixed directory prefix before the first glob character.
            prefix: list[str] = []
            for part in pattern.split("/"):
                if any(c in _GLOB_CHARS for c in part):
                    break
                prefix.append(part)
            out.append("/".join(prefix) or "/")
    return out


def _webfetch_ok(inp: Mapping[str, Any]) -> None:
    import ipaddress
    from urllib.parse import urlsplit
    url = inp.get("url")
    if not isinstance(url, str) or not url:
        raise _Refused("WebFetch needs a url")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise _Refused("WebFetch is https-only in a sandboxed session")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _Refused("WebFetch URLs may not carry credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise _Refused("WebFetch URL has an invalid port") from exc
    if port not in (None, 443):
        raise _Refused("WebFetch may use only the https port")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or not host.isascii():
        raise _Refused("WebFetch URL has no usable host")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise _Refused("WebFetch may not fetch an IP address")
    for domain in ALLOWED_DOMAINS:
        if domain.startswith("*."):
            if host.endswith(domain[1:]) and host != domain[2:]:
                return
        elif host == domain:
            return
    raise _Refused(f"WebFetch is limited to {', '.join(ALLOWED_DOMAINS)}; {host!r} is not")


def confine_tool(tool_name: str, tool_input: Any, cwd: str | None, *,
                 tmpdir: str | None = None) -> str | None:
    """Why a sandboxed session may not run this tool call, or None when it may.
    Deny-by-default (fix round 3):

    * :data:`PLAIN_TOOLS` and ViberPit's own MCP tools: allowed.
    * Edit / Write / MultiEdit / NotebookEdit: one unambiguous path inside the worktree,
      no symlink component, no extra hard link, not protected (``.git``, ``.claude``,
      secrets: ``patterns.is_protected_path``). Serialising writes against running
      shells is :class:`SandboxGate`'s job, not this pure check's.
    * :data:`READ_TOOLS`: inside the worktree (not protected), the clone's common git
      dir or the Claude temp root (*tmpdir*), symlink-safe.
    * WebFetch: https, no userinfo, no IP literal, host in :data:`ALLOWED_DOMAINS`.
    * Everything else — WebSearch, third-party MCP tools, Skill, Monitor … — denied.
    """
    from server.mcp_spec import is_mcp_tool
    from server.permission_rules.patterns import FILE_TOOLS, is_protected_path, tool_path_arg
    inp = tool_input if isinstance(tool_input, Mapping) else {}
    if not cwd:
        return f"{tool_name} cannot be checked: the session has no worktree"
    try:
        if tool_name in PLAIN_TOOLS or is_mcp_tool(tool_name):
            return None
        if tool_name in FILE_TOOLS:
            raw = tool_path_arg(tool_name, inp)
            if raw is None:
                raise _Refused("it needs one unambiguous file path")
            _path, rel = _resolve_in_worktree(raw, cwd)
            if is_protected_path(rel):
                raise _Refused(f"{raw!r} is protected (git internals, .claude settings "
                               "and hooks, and secrets cannot be changed)")
            return None
        if tool_name in READ_TOOLS:
            for raw in _read_paths(tool_name, inp):
                _read_target_ok(raw, cwd, tmpdir)
            return None
        if tool_name == "WebFetch":
            _webfetch_ok(inp)
            return None
    except _Refused as exc:
        return f"{tool_name} is not allowed in a sandboxed session: {exc}"
    return (f"{tool_name} is not available in a sandboxed session (only the shell, file "
            "tools inside the worktree, GitHub web fetches and the built-in session "
            "tools are)")


#: The retry-friendly refusals the gate answers with.
WRITE_WAITS_FOR_COMMAND = ("Another command is running in this session; retry this edit "
                           "when it finishes.")
COMMAND_WAITS_FOR_WRITE = ("A file edit is being written in this session; retry this "
                           "command when it finishes.")
WRITE_STATE_UNKNOWN = ("This session's running commands are not known yet; retry this "
                       "edit in a moment.")


class SandboxGate:
    """One sandboxed CLI's in-flight registry (owner decision 2026-11-07: serialise
    writes against Bash). The CLI performs Edit/Write in its own host process after the
    PreToolUse check, so a sandboxed shell running at that moment — in ANY thread: the
    main agent or a sub-agent, foreground or background — could swap a symlink in under
    the write. So a file write is admitted only while no Bash is in flight, and a Bash
    only while no write is.

    **Why no lock object.** Every PreToolUse/PostToolUse callback of one CLI arrives on
    the driver's single event loop, and :meth:`admit` checks and registers with no
    ``await`` in between: check-then-register is atomic by construction, so "no Bash in
    flight, allow the write" and "register a new Bash" cannot interleave. A Bash that
    arrives while an admitted write is pending is REFUSED (not parked): waiting inside a
    hook would hold the CLI's hook call open under its timeout, and the model retries a
    refusal it can read.

    **Entries.** ``bash`` / ``writes`` are foreground calls keyed by ``tool_use_id``,
    sub-agents' included (the hook sees every thread: ``e8t``'s only gate is ``q0``
    @295954344). Each is cleared by its ``PostToolUse`` or ``PostToolUseFailure`` hook,
    by its tool-result event (a call the permission flow denied after our hook gets no
    PostToolUse, but always a result), and wholesale at turn end and on interrupt.
    ``background`` holds launched background work — a Bash with ``run_in_background``
    (seen by the hook) or any backgrounded task the CLI reports — cleared only by its
    end event, never by a turn end (it outlives turns) and never on a timeout guess: a
    lost end event refuses writes until the session is reloaded (fail closed). A new CLI
    starts with an empty gate truthfully: every sandboxed process dies with the CLI
    that spawned it (bwrap ``--die-with-parent`` @288416156, ``--unshare-pid``
    @288418124).

    **Unknown state refuses.** Until the driver's start has verified the sandbox, and
    after its event stream is lost, :attr:`known` is False and writes are refused.
    """

    def __init__(self, alive: Callable[[], bool] | None = None) -> None:
        self.bash: set[str] = set()
        self.writes: set[str] = set()
        self.background: set[str] = set()
        self.known = False
        #: The owning driver's "is my event stream live?" — a lost stream means lost
        #: PostToolUse/end events, so the registry is no longer known.
        self.alive = alive

    def admit(self, tool_name: str, tool_input: Any, tool_use_id: str | None) -> str | None:
        """Register this call, or say why it must wait. Synchronous on purpose."""
        from server.permission_rules.patterns import FILE_TOOLS
        tid = tool_use_id or f"anon-{id(tool_input)}"
        if tool_name == "Bash":
            if self.writes:
                return COMMAND_WAITS_FOR_WRITE
            self.bash.add(tid)
            if isinstance(tool_input, Mapping) and tool_input.get("run_in_background"):
                self.background.add(tid)
            return None
        if tool_name in FILE_TOOLS:
            reason = self.write_blocked()
            if reason is None:
                self.writes.add(tid)
            return reason
        return None

    def write_blocked(self) -> str | None:
        """Why a file write may not run now, without registering anything."""
        if not self.known or (self.alive is not None and not self.alive()):
            return WRITE_STATE_UNKNOWN
        if self.bash or self.background:
            return WRITE_WAITS_FOR_COMMAND
        return None

    def done(self, tool_use_id: str | None) -> None:
        """A call finished (PostToolUse / PostToolUseFailure / its tool result). A
        background launch's own entry stays until its task ends."""
        if tool_use_id:
            self.bash.discard(tool_use_id)
            self.writes.discard(tool_use_id)

    def background_started(self, tool_use_id: str) -> None:
        self.background.add(tool_use_id)

    def background_ended(self, tool_use_id: str) -> None:
        self.background.discard(tool_use_id)

    def end_turn(self) -> None:
        """Turn end / interrupt: no foreground call outlives it (a lost event cannot
        wedge writes past it). Background work does outlive it, so it is kept."""
        self.bash.clear()
        self.writes.clear()


def confinement_hook(cwd: str | None, *, tmpdir: str | None = None,
                     gate: SandboxGate | None = None):
    """The SDK ``PreToolUse`` callback: :func:`confine_tool`, then the gate."""
    async def _hook(input_data, tool_use_id=None, context=None) -> dict:
        data = input_data or {}
        tool = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        reason = confine_tool(tool, tool_input, cwd, tmpdir=tmpdir)
        if reason is None and gate is not None:
            reason = gate.admit(tool, tool_input, tool_use_id or data.get("tool_use_id"))
        if reason is None:
            return {}
        log.info("sandbox.tool_denied tool=%s", tool)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    return _hook


def done_hook(gate: SandboxGate):
    """The SDK ``PostToolUseFailure`` callback: a failed call leaves the gate."""
    async def _hook(input_data, tool_use_id=None, context=None) -> dict:
        gate.done(tool_use_id or (input_data or {}).get("tool_use_id"))
        return {}
    return _hook


def written_path_escaped(tool_name: str, tool_input: Any, cwd: str | None) -> str | None:
    """After a file tool ran: the path it wrote when that path now resolves OUTSIDE the
    worktree (a symlink swapped in between the check and the write), else None."""
    from server.permission_rules.patterns import FILE_TOOLS, tool_path_arg
    if tool_name not in FILE_TOOLS or not cwd:
        return None
    raw = tool_path_arg(tool_name, tool_input if isinstance(tool_input, Mapping) else {})
    if raw is None:
        return None
    base = os.path.realpath(cwd)
    target = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
    return None if _within(target, base) and target != base else target


def post_write_hook(cwd: str | None, on_escape: Callable[[str, str], Any],
                    gate: SandboxGate | None = None):
    """The SDK ``PostToolUse`` callback: the call leaves the gate, and a file tool whose
    target now resolves outside the worktree calls ``on_escape(tool_name,
    resolved_path)`` (the orchestrator holds the session ``sandbox_escape_suspected``)."""
    async def _hook(input_data, tool_use_id=None, context=None) -> dict:
        data = input_data or {}
        tool = str(data.get("tool_name") or "")
        if gate is not None:
            # Before the escape check: a held session must not keep a stale entry.
            gate.done(tool_use_id or data.get("tool_use_id"))
        escaped = written_path_escaped(tool, data.get("tool_input") or {}, cwd)
        if escaped is not None:
            res = on_escape(tool, escaped)
            if asyncio.iscoroutine(res):
                await res
        return {}
    return _hook


# ── per-session temp root (fix round 5) ──────────────────────────────────────
# The CLI's temp root is ``CLAUDE_CODE_TMPDIR`` (2.1.319 ``Xf()`` @282248627:
# ``CLAUDE_CODE_TMPDIR``, else ``os.tmpdir()``); it is the sandbox's default writable temp
# root (spike §1.6, ``$m()`` @288608093) and the sandboxed shell's ``TMPDIR``
# (``Lpt`` @286500575). The shared root holds every session's Bash output, so a
# sandboxed session gets its own, and only that one is re-allowed and writable.

#: The directory, beside the shared Claude temp root, that holds per-session roots.
SESSION_TMP_PARENT = "viberpit-sbx"


def session_tmp_path(session_id: str) -> str:
    """``<dir of the shared Claude temp root>/viberpit-sbx/<session_id>`` — off the
    ``/tmp`` tmpfs, like the shared root (``claude_sdk_factory.session_tmpdir_path``)."""
    from server.sessions.claude_sdk_factory import session_tmpdir_path
    if not session_id or os.sep in session_id or session_id in (".", ".."):
        raise ValueError(f"bad session id {session_id!r}")
    return os.path.join(os.path.dirname(os.path.abspath(session_tmpdir_path())),
                        SESSION_TMP_PARENT, session_id)


def prepare_session_tmp(session_id: str) -> str:
    """Create the session's temp root 0700 (its parent too) and prove it is a real
    directory owned by this user — never a symlink a racing process planted."""
    import stat as _stat
    path = session_tmp_path(session_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SandboxError(SANDBOX_START_FAILED,
                           "the session's sandbox temp directory is not a private directory")
    os.chmod(path, 0o700)
    return path


def remove_session_tmp(session_id: str) -> None:
    """Remove the session's temp root (driver close: session close, archive, respawn).
    Best-effort: a leftover is re-made private at the next spawn."""
    import shutil
    try:
        path = session_tmp_path(session_id)
    except ValueError:
        return
    if os.path.islink(path):
        os.unlink(path)
        return
    shutil.rmtree(path, ignore_errors=True)


#: Re-allows under the HOME read-deny (fix round 4): toolchains a sandboxed shell runs
#: and the git config it reads. Each entry is home-relative and is ONLY a re-allow of a
#: path the home deny hides — nothing outside home is denied by it.
TOOLCHAIN_READS: tuple[str, ...] = (
    ".local/bin",          # user-installed tools (uv, gh, …)
    ".local/share/uv",     # uv-managed Pythons and tools
    ".cache/uv",           # uv's package cache (read; writes stay denied)
    ".nvm",                # node versions
    ".npm",                # npm cache (read)
    ".cargo",              # rust toolchain bins
    ".rustup",
    ".pyenv",
    ".gitconfig",          # git identity and settings
    ".config/git",
)


def _daemon_dirs() -> list[str]:
    """ViberPit's own config and state dirs, denied to a sandboxed shell: the host
    paths (``hostpaths``: /etc, /run, /var/lib, /var/log stems, canonical and legacy),
    the account store (``VIBERPIT_HOME``), the log and archive dirs and a SQLite DB's
    dir, read from the settings."""
    from viberpit import brand, paths
    from server import hostpaths
    from server.config import get_settings
    out = [hostpaths.ETC_DIR, hostpaths.RUN_DIR, hostpaths.STATE_DIR, hostpaths.LOG_DIR]
    for stem in ("/etc", "/run", "/var/lib", "/var/log"):
        out += [f"{stem}/{brand.HOST_SLUG}", f"{stem}/{brand.LEGACY_SLUG}"]
    out.append(str(paths.root()))
    cfg = get_settings()
    for d in (getattr(cfg, "log_dir", None), getattr(cfg, "archive_home", None)):
        if d:
            out.append(os.path.expanduser(str(d)))
    url = str(getattr(cfg, "db_url", "") or "")
    if url.startswith("sqlite") and ":///" in url:
        db_path = url.split(":///", 1)[1]
        if db_path and db_path != ":memory:":
            out.append(os.path.dirname(os.path.abspath(db_path)))
    return list(dict.fromkeys(os.path.abspath(p) for p in out))


def read_policy(config_dir: str | None, cwd: str | None = None,
                tmpdir: str | None = None) -> tuple[list[str], list[str]]:
    """``(denyRead, allowRead)`` for a sandboxed shell (bwrap ro-binds ``/``).

    Deny the whole HOME directory and ViberPit's own dirs (:func:`_daemon_dirs`), then
    re-allow only what the session needs — ``allowRead`` "takes precedence over
    denyRead" (2.1.319 schema @282524692) and becomes the Linux mount builder's
    ``allowWithinDeny`` (@288304132; bind logic @288404941):

    * the worktree (its realpath) and the clone's common git dir;
    * ``<CLAUDE_CONFIG_DIR>/shell-snapshots`` (the Bash tool sources its snapshot from
      there before every command: PATH, functions, shell options — no secrets);
    * the gh config git authenticates with (``session_env``'s ``GH_CONFIG_DIR``, else the
      ambient file-backed one);
    * the session's Claude temp root (the Bash output files);
    * :data:`TOOLCHAIN_READS`.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    deny = list(dict.fromkeys([home, *_daemon_dirs()]))
    allow: list[str] = []
    if cwd:
        allow.append(os.path.realpath(cwd))
        common = git_common_dir(cwd)
        if common:
            allow.append(os.path.realpath(common))
    if config_dir:
        allow.append(os.path.join(os.path.realpath(config_dir), "shell-snapshots"))
    dedicated = _viberpit_gh_config_dir()
    gh = (dedicated if dedicated and gh_file_backed(dedicated)
          else _ambient_gh_config_dir())
    allow.append(os.path.realpath(gh))
    if tmpdir:
        allow.append(os.path.realpath(tmpdir))
    allow += [os.path.join(home, rel) for rel in TOOLCHAIN_READS]
    return deny, list(dict.fromkeys(allow))


def startup_failure(texts: Iterable[str | None]) -> str | None:
    """The CLI's own sandbox refusal among *texts* (an exception chain's messages, the
    SDK's last error result, captured stderr lines), or None when none is one. Returns
    the first matching line, stripped — the message the session shows."""
    for text in texts:
        if not text:
            continue
        for line in str(text).splitlines():
            low = line.lower()
            if any(m in low for m in _STARTUP_MARKERS):
                return line.strip()[:500]
    return None


# ── host preflight ───────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclasses.dataclass(frozen=True)
class Preflight:
    """One host's sandbox readiness. ``reason`` is the first failure's ``detail``."""
    ok: bool
    reason: str | None
    checks: tuple[PreflightCheck, ...]
    cli_version: str | None = None
    diagnostics: dict[str, str | None] = dataclasses.field(default_factory=dict)
    checked_at: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        from datetime import datetime, timezone
        from server.time_iso import iso_utc
        at = (datetime.fromtimestamp(self.checked_at, timezone.utc).replace(tzinfo=None)
              if self.checked_at else None)
        return {"ok": self.ok, "reason": self.reason,
                "checks": [c.to_wire() for c in self.checks],
                "cliVersion": self.cli_version, "diagnostics": dict(self.diagnostics),
                "checkedAt": iso_utc(at)}


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

#: Read for diagnosis only — the namespace probe is the gate (spike §2.3 step 3).
_SYSCTLS: tuple[tuple[str, str], ...] = (
    ("kernel.unprivileged_userns_clone", "/proc/sys/kernel/unprivileged_userns_clone"),
    ("user.max_user_namespaces", "/proc/sys/user/max_user_namespaces"),
    ("kernel.apparmor_restrict_unprivileged_userns",
     "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
)


def _version(text: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _dotted(v: tuple[int, int, int]) -> str:
    return ".".join(map(str, v))


def _read_sysctls() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name, path in _SYSCTLS:
        try:
            with open(path, encoding="ascii") as fh:
                out[name] = fh.read().strip()
        except OSError:
            out[name] = None
    return out


def _default_cli_version() -> str | None:
    from server.sessions.claude_bin import cli_version
    v = cli_version()
    return _dotted(v) if v else None


Runner = Callable[..., Any]


def _run(run: Runner, argv: list[str]) -> tuple[int | None, str]:
    """One argv-only subprocess under :data:`PROBE_TIMEOUT_S`: ``(rc, output)``, rc None
    when it timed out or could not run."""
    try:
        cp = run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {PROBE_TIMEOUT_S:g}s"
    except OSError as exc:
        return None, str(exc)
    return cp.returncode, ((cp.stdout or "") + (cp.stderr or "")).strip()


def run_preflight(*, which: Callable[[str], str | None] = shutil.which,
                  run: Runner = subprocess.run,
                  cli_version: Callable[[], str | None] = _default_cli_version,
                  sysctls: Callable[[], dict[str, str | None]] = _read_sysctls,
                  clock: Callable[[], float] = time.time) -> Preflight:
    """The spike §2.3 checks, in order, all run (so the card can list each); ``reason``
    is the first failure. Blocking — call through :func:`preflight`."""
    checks: list[PreflightCheck] = []
    bwrap = which("bwrap")
    checks.append(PreflightCheck("bwrap", bwrap is not None,
                                 None if bwrap else "bubblewrap (bwrap) is not installed"))
    if bwrap:
        rc, out = _run(run, [bwrap, "--version"])
        v = _version(out) if rc == 0 else None
        if v is None:
            checks.append(PreflightCheck("bwrap_version", False,
                                         f"bwrap --version could not be read ({out})"))
        elif v < MIN_BWRAP_VERSION:
            checks.append(PreflightCheck("bwrap_version", False,
                                         f"bubblewrap {_dotted(v)} is older than "
                                         f"{_dotted(MIN_BWRAP_VERSION)}"))
        else:
            checks.append(PreflightCheck("bwrap_version", True, _dotted(v)))
    socat = which("socat")
    checks.append(PreflightCheck("socat", socat is not None,
                                 None if socat else "socat is not installed"))
    if bwrap:
        rc, out = _run(run, [bwrap, "--ro-bind", "/", "/", "--dev", "/dev",
                             "--unshare-net", "true"])
        checks.append(PreflightCheck(
            "bwrap_probe", rc == 0,
            None if rc == 0 else
            f"bwrap could not create a network namespace ({out or f'exit {rc}'})"))
    dedicated = _viberpit_gh_config_dir()
    gh_ok = ((bool(dedicated) and gh_file_backed(dedicated))
             or gh_file_backed(_ambient_gh_config_dir()))
    checks.append(PreflightCheck(
        "gh", gh_ok,
        None if gh_ok else "gh token is keyring-only; sandboxed git cannot authenticate"))
    cli = cli_version()
    cv = _version(cli)
    if cv is None:
        checks.append(PreflightCheck("cli_version", False,
                                     "the claude CLI version could not be read"))
    elif cv < MIN_CLI_VERSION:
        checks.append(PreflightCheck("cli_version", False,
                                     f"claude CLI {_dotted(cv)} is older than the verified "
                                     f"{_dotted(MIN_CLI_VERSION)}"))
    else:
        checks.append(PreflightCheck("cli_version", True, _dotted(cv)))
    failed = next((c for c in checks if not c.ok), None)
    return Preflight(ok=failed is None, reason=failed.detail if failed else None,
                     checks=tuple(checks), cli_version=_dotted(cv) if cv else None,
                     diagnostics=sysctls(), checked_at=clock())


_cache: Preflight | None = None
_lock: asyncio.Lock | None = None


def cached_preflight() -> Preflight | None:
    """This host's last preflight, without running one."""
    return _cache


async def preflight(*, refresh: bool = False) -> Preflight:
    """This host's preflight: the cached one, or a fresh run when there is none or
    *refresh* asks. Runs off the loop; concurrent callers share one run."""
    global _cache, _lock
    if _cache is not None and not refresh:
        return _cache
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _cache is not None and not refresh:
            return _cache
        result = await asyncio.to_thread(run_preflight)
        _cache = result
    # INFO either way: a host without socat is a known, reported condition (the toggle
    # says so), not an anomaly to auto-file.
    log.info("sandbox.preflight ok=%s reason=%s cli=%s", result.ok, result.reason,
             result.cli_version)
    return result


def reset_preflight_cache() -> None:
    """Forget the cached preflight (tests)."""
    global _cache, _lock
    _cache = None
    _lock = None


# ── what a session serves ────────────────────────────────────────────────────


def session_view(record: Any, caps: Any) -> dict[str, Any]:
    """The browser's ``sandbox`` object for one session: ``{enabled, state, reason}``.
    ``state`` is ``off`` unless the session can and does carry one; then the last
    verification (``pending`` until this CLI is verified)."""
    if not takes_sandbox(record, caps) or getattr(record, "sandbox", None) is not True:
        return {"enabled": False, "state": STATE_OFF, "reason": None}
    # The kill switch does not turn a recorded-on session "off": it is refused (held).
    state = getattr(record, "sandbox_state", None) or STATE_PENDING
    return {"enabled": True, "state": state,
            "reason": getattr(record, "sandbox_reason", None) if state == STATE_FAILED
            else None}


async def read_default() -> bool:
    """``AppSettings.sandbox_default`` (NULL = off). A failed read RAISES: the create
    that asked fails rather than recording ``sandbox=False`` for a session the owner
    wanted sandboxed (fail closed)."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import AppSettings
    async with db_session() as db:
        value = (await db.execute(sa_select(AppSettings.sandbox_default).where(
            AppSettings.id == "singleton"))).scalar_one_or_none()
    return value is True


async def require_here(caps: Any) -> None:
    """Refuse unless THIS host can run a sandboxed session on an engine with *caps* now:
    the kill switch is on, the engine declares ``sandbox`` and the host preflight passes.
    For a session that must be born sandboxed (a sandboxed parent's child or fork):
    refused up front rather than created and then held."""
    if not sandbox_enabled():
        raise SandboxError(SANDBOX_DISABLED, "the sandbox is turned off on this host")
    if caps is None or not getattr(caps, "sandbox", False):
        raise SandboxError(SANDBOX_UNSUPPORTED,
                           "this session's engine cannot run in the sandbox")
    pf = await preflight()
    if not pf.ok:
        raise SandboxError(SANDBOX_UNAVAILABLE, pf.reason)


async def host_readiness() -> dict[str, Any]:
    """This host's answer to "can a sandboxed session run here?" for a move preflight
    (D-17 x D-23): ``{ok, reason}``. The kill switch off is a refusal like a failed
    preflight."""
    if not sandbox_enabled():
        return {"ok": False, "reason": "the sandbox is turned off on this host"}
    pf = await preflight()
    return {"ok": pf.ok, "reason": pf.reason}
