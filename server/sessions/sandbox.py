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


def settings_block() -> dict[str, Any]:
    """``{"sandbox": {...}}`` — the full block for one spawn's ``--settings`` JSON (spike
    §2.1). A fresh deep copy every call: nothing a caller does to it reaches the next."""
    return {SANDBOX_KEY: copy.deepcopy(_SETTINGS_BLOCK)}


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


def _under(path: str, roots: Iterable[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def git_common_dir(cwd: str | None) -> str | None:
    """The worktree's common git dir (``<clone>/.git``), which the CLI grants write
    automatically (spike §1.6, ``fwr()`` @288687776), or None."""
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


#: The CLI's own default task-dir prefix (``/tmp/claude-<uid>``), its child-process
#: tmpdir when ``CLAUDE_CODE_TMPDIR`` is unset (spike §1.6, ``hAe()``).
_CLI_TMP_PREFIX = os.path.join(os.sep, "tmp", "claude")


def writable_roots(cwd: str | None, tmpdir: str | None) -> tuple[str, ...]:
    """Where a sandboxed session may write: its worktree, that worktree's common git
    dir, the Claude temp root, and the CLI's own ``/tmp/claude*`` tmpdir."""
    roots = [r for r in (cwd, git_common_dir(cwd), tmpdir) if r]
    return tuple(roots)


def _write_allowed(path: str, roots: tuple[str, ...]) -> bool:
    if path in (".", "./"):
        return True
    if os.path.realpath(path).startswith(_CLI_TMP_PREFIX):
        return True
    return _under(path, roots)


def check_dialog(resp: Any, *, cwd: str | None = None,
                 tmpdir: str | None = None) -> DialogVerdict:
    """Require every condition of spike §2.4 — and that the restrictions the CLI says it
    applied are no wider than ViberPit's block: no excluded command, no domain beyond
    :data:`ALLOWED_DOMAINS`, no writable path beyond :func:`writable_roots`. A missing
    §2.4 field fails: the request is ``@internal`` and a reply shape it does not
    recognise is not proof of a sandbox. The restriction lists are checked when the
    reply carries them (spike §1.2: ``excluded_commands``,
    ``restrictions.{network_allowed_domains, fs_allow_write}``)."""
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
    excluded = resp.get("excluded_commands")
    if excluded is not None and excluded != []:
        return DialogVerdict(False, "commands are excluded from the sandbox: "
                             + ", ".join(map(str, excluded))
                             if isinstance(excluded, list) else
                             "the excluded-commands list is unreadable")
    domains = restrictions.get("network_allowed_domains")
    if domains is not None:
        if not isinstance(domains, list):
            return DialogVerdict(False, "the network allowlist is unreadable")
        extra = [str(d) for d in domains if d not in ALLOWED_DOMAINS]
        if extra:
            return DialogVerdict(False, "the network allowlist is wider than GitHub: "
                                 + ", ".join(extra))
    writes = restrictions.get("fs_allow_write")
    if writes is not None:
        if not isinstance(writes, list):
            return DialogVerdict(False, "the writable paths are unreadable")
        roots = writable_roots(cwd, tmpdir)
        wide = [str(w) for w in writes if not _write_allowed(str(w), roots)]
        if wide:
            return DialogVerdict(False, "writable paths beyond the worktree: "
                                 + ", ".join(wide))
    return DialogVerdict(True)


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
