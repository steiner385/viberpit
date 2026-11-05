"""The ONE writer of Claude plugin state (D-16c): ``claude plugin … --json`` on this host.

Every plugin and marketplace mutation — install, update, uninstall, enable/disable,
marketplace add/update/remove — goes through :func:`run_op`, on every host, through the
daemon RPC ``plugin_op``. Nothing in ViberPit writes ``enabledPlugins`` or the plugin
registry itself any more (Principle VI).

Binding CLI facts (docs/superpowers/specs/2026-11-04-d16-verification.md §1, re-probed on
2.1.318 for this module):

* Each ``--json`` mutation prints one result object. It is the first stdout line, EXCEPT
  when the CLI shows a marketplace-declared command: the human prompt (which quotes that
  marketplace-controlled command) comes first. So the first line is parsed, falling back
  to the line carrying this run's ``CLAUDE_CODE_RESULT_NONCE``; a line whose ``nonce``
  disagrees is a forgery, and any line naming a command gate refuses the run.
* A no-op exits 1 with a ``failureCode`` (``already_in_goal_state``, ``not_installed``,
  ``not_configured``), so outcomes key on ``failureCode``, never on the exit code.
* ``installPath`` is absolute, so the CLI runs with ``CLAUDE_CONFIG_DIR=shared_base()``:
  the registry then names ``~/.claude/plugins/cache/…`` and survives account removal.

Never passed: ``-y``, ``--accept-command`` (consent to a marketplace command),
``--sparse``, ``--claudeai``, ``--marketplace`` (adds a marketplace at install time),
``--prune``. Scope is always ``user``.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from viberpit import paths
from server.library import plugin_policy
from server.library.names import valid_plugin_id

log = logging.getLogger(__name__)

#: One CLI run: a marketplace add/update clones a git repo.
CLI_TIMEOUT_S = 180.0
#: The edge's deadline for one host's ``plugin_op`` RPC: the CLI's own, plus transport.
RPC_TIMEOUT_S = CLI_TIMEOUT_S + 20.0
#: ``plugin list --available`` refreshes the Directory cache at most this often (≥ 1 h).
AVAILABLE_REFRESH_TTL_S = 3600.0

FORBIDDEN_FLAGS = frozenset({"-y", "--yes", "--accept-command", "--sparse", "--claudeai",
                             "--marketplace", "--prune"})

#: The failure codes 2.1.318 reports for a marketplace-declared command it would not run
#: (``he`` @311921385), plus a ``shownCommand`` on the line.
COMMAND_GATE_CODES = frozenset({"command_source_refused", "command_source_declined",
                                "entry_helper_unconfirmed", "entry_helper_declined"})

# op -> (argv after ``plugin``, the arg it takes, failure codes that mean "already so").
# Per op: ``not_installed`` is "done" for uninstall but a real failure for enable.
_USER = ("--scope", "user")
_OPS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], frozenset[str]]] = {
    "marketplace_add": (("marketplace", "add"), "source", _USER, frozenset()),
    "marketplace_update": (("marketplace", "update"), "name", (), frozenset()),
    "marketplace_remove": (("marketplace", "remove"), "name", _USER,
                           frozenset({"not_configured"})),
    "install": (("install",), "plugin_id", _USER, frozenset({"already_in_goal_state"})),
    "update": (("update",), "plugin_id", _USER, frozenset({"already_in_goal_state"})),
    "uninstall": (("uninstall",), "plugin_id", (*_USER, "--keep-data"),
                  frozenset({"not_installed", "already_in_goal_state"})),
    "enable": (("enable",), "plugin_id", _USER, frozenset({"already_in_goal_state"})),
    "disable": (("disable",), "plugin_id", _USER, frozenset({"already_in_goal_state"})),
}
OPS = frozenset(_OPS)

# Pass-through env: what git (marketplace clones, credential helpers) and TLS need. No
# API keys, no OAuth tokens, no CLAUDE_* of the calling process.
_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                    "https_proxy", "http_proxy", "no_proxy")
_MESSAGE_CAP = 500


class InvalidPluginOp(ValueError):
    """An op/args pair the wrapper refuses to turn into argv."""


def _claude_bin() -> str | None:
    from server.sessions.claude_bin import resolve_claude_bin
    return resolve_claude_bin()


def _run(argv: list[str], *, env: dict[str, str], cwd: str, timeout: float) -> tuple[int, str]:
    """The subprocess seam: ``(exit code, stdout)``. An argv list, never a shell."""
    proc = subprocess.run(argv, env=env, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout or ""


def _cwd() -> str:
    # Never the daemon's checkout: the CLI treats its cwd as a project.
    from server.sessions.helper_call import helper_cwd
    return helper_cwd()


def _env(nonce: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update({
        "HOME": str(Path.home()),
        "CLAUDE_CONFIG_DIR": str(paths.shared_base()),
        "CLAUDE_CODE_RESULT_NONCE": nonce,
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
    })
    return env


def build_argv(binary: str, op: str, args: dict[str, Any]) -> list[str]:
    """argv for *op*, re-validating every argument here (defense in depth: this runs on
    the daemon, whatever the edge checked)."""
    spec = _OPS.get(op)
    if spec is None:
        raise InvalidPluginOp(f"unknown plugin op {op!r}")
    words, key, flags, _noop = spec
    value = args.get(key) if isinstance(args, dict) else None
    if not isinstance(value, str):
        raise InvalidPluginOp(f"{op} needs {key}")
    if key == "plugin_id" and not valid_plugin_id(value):
        raise InvalidPluginOp(f"invalid plugin id {value!r}")
    if key == "name" and not plugin_policy.valid_marketplace_name(value):
        raise InvalidPluginOp(f"invalid marketplace name {value!r}")
    if key == "source":
        try:
            value = plugin_policy.check_marketplace_source(value)
        except plugin_policy.PolicyRefusal as exc:
            raise InvalidPluginOp(exc.message) from None
    argv = [binary, "plugin", *words, value, *flags, "--json"]
    assert_safe_argv(argv)
    return argv


def assert_safe_argv(argv: list[str]) -> None:
    """The last line of defense for D1: no consent-bypass flag ever reaches the CLI, and
    the one free argument can never be read as a flag."""
    bad = [a for a in argv if a in FORBIDDEN_FLAGS or a.split("=", 1)[0] in FORBIDDEN_FLAGS]
    if bad:
        raise InvalidPluginOp(f"forbidden flag(s) {bad}")
    if argv[4 if argv[2] == "marketplace" else 3].startswith("-"):
        raise InvalidPluginOp("argument looks like a flag")


def _outcome(outcome: str, failure_code: str | None = None, message: str | None = None,
             **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"outcome": outcome}
    if failure_code is not None:
        out["failureCode"] = failure_code
    if message:
        out["message"] = message[:_MESSAGE_CAP]
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def parse_result(op: str, rc: int, stdout: str, nonce: str) -> dict[str, Any]:
    """One host's outcome from one CLI run. Fails closed: no readable result line, a
    nonce that is not ours, or ``outcome: ok`` with a non-zero exit is ``failed``; any
    line naming a command gate refuses the run whatever else was printed."""
    lines = stdout.splitlines()
    first: dict[str, Any] | None = None
    if lines:
        try:
            parsed = json.loads(lines[0])
            first = parsed if isinstance(parsed, dict) else None
        except ValueError:
            first = None
    candidates = _json_lines(stdout)
    if any("shownCommand" in c or c.get("failureCode") in COMMAND_GATE_CODES for c in candidates):
        return _outcome("failed", plugin_policy.COMMAND_SOURCE_REFUSED,
                        "This plugin installs by running a command its marketplace declares. "
                        "ViberPit never accepts those.")
    # The line carrying OUR nonce is the CLI's own; the first line is the fallback for a
    # CLI too old to echo the nonce (rocky ran 2.1.293).
    result = next((c for c in candidates if c.get("nonce") == nonce), first)
    if result is None:
        return _outcome("failed", "unparseable_result",
                        f"The claude CLI printed no readable result (exit {rc}).")
    if "nonce" in result and result.get("nonce") != nonce:
        return _outcome("failed", "result_nonce_mismatch",
                        "The CLI result line did not carry this run's nonce.")
    message = result.get("message") if isinstance(result.get("message"), str) else None
    code = result.get("failureCode") if isinstance(result.get("failureCode"), str) else None
    if result.get("outcome") == "ok":
        if rc != 0:
            return _outcome("failed", "exit_mismatch", f"outcome ok with exit {rc}")
        return _outcome("ok", None, message,
                        version=result.get("newVersion") or result.get("installedVersion"),
                        marketplace=result.get("marketplace")
                        if op.startswith("marketplace_") else None)
    if result.get("outcome") == "failed" and code in _OPS[op][3]:
        return _outcome("noop", code, message)
    return _outcome("failed", code or "op_failed", message)


def _installed_version(plugin_id: str) -> str | None:
    from server.library.scan import installed_registry, newest_install
    version = newest_install(installed_registry().get(plugin_id, [])).get("version")
    return version if isinstance(version, str) else None


def run_op(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one plugin op on THIS host and report ``{outcome, failureCode?, message?,
    version?, marketplace?}``. Blocking: callers run it in a thread. Never raises for an
    expected failure; a refused op is ``failed`` with ``invalid_args``."""
    binary = _claude_bin()
    if binary is None:
        return _outcome("failed", "claude_not_found", "No claude binary resolves on this host.")
    try:
        argv = build_argv(binary, op, args)
    except InvalidPluginOp as exc:
        log.info("plugin_cli.refused op=%s reason=%s", op, exc)
        return _outcome("failed", "invalid_args", str(exc))
    nonce = secrets.token_hex(16)
    try:
        rc, stdout = _run(argv, env=_env(nonce), cwd=_cwd(), timeout=CLI_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.info("plugin_cli.timeout op=%s", op)
        return _outcome("failed", "cli_timeout", f"claude plugin {op} exceeded {CLI_TIMEOUT_S:.0f}s.")
    except OSError as exc:
        log.warning("plugin_cli.spawn_failed op=%s error=%s", op, exc)
        return _outcome("failed", "cli_spawn_failed", str(exc))
    out = parse_result(op, rc, stdout, nonce)
    log.info("plugin_cli.result op=%s outcome=%s code=%s", op, out["outcome"],
             out.get("failureCode"))
    if out["outcome"] in ("ok", "noop") and op in ("install", "update") and "version" not in out:
        version = _installed_version(str(args.get("plugin_id")))
        if version:
            out["version"] = version
    return out


_last_available_refresh = 0.0


def refresh_available(*, force: bool = False) -> bool:
    """Run ``plugin list --json --available`` on this host so the CLI (re)fetches the
    Directory cache (``plugin-directory-cache-v2.json``). Its ~7 MB stdout is discarded:
    browse reads the cache file. At most once per ``AVAILABLE_REFRESH_TTL_S`` unless
    *force*. Returns whether a run happened and exited 0."""
    global _last_available_refresh
    now = time.monotonic()
    if not force and _last_available_refresh and now - _last_available_refresh < AVAILABLE_REFRESH_TTL_S:
        return False
    _last_available_refresh = now
    binary = _claude_bin()
    if binary is None:
        return False
    argv = [binary, "plugin", "list", "--json", "--available"]
    try:
        rc, _ = _run(argv, env=_env(secrets.token_hex(16)), cwd=_cwd(), timeout=CLI_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.info("plugin_cli.available_refresh_failed error=%s", exc)
        return False
    return rc == 0


async def host_outcome(host: str, call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Await one host's ``plugin_op`` and label it. A host that cannot answer is a
    ``failed`` outcome for THAT host — never an exception for the fan-out: an old daemon
    (``UnknownMethod``) is ``daemon_outdated``, an unreachable one ``host_unreachable``."""
    from server.ipc.client import DaemonError, DaemonTimeout, DaemonUnavailable
    try:
        res = await call
    except DaemonError as exc:
        if exc.err_type == "UnknownMethod":
            return {"host": host, **_outcome("failed", "daemon_outdated",
                                             "This host's daemon predates plugin management; "
                                             "update it, then retry.")}
        log.warning("plugin_cli.host_error host=%s type=%s", host, exc.err_type)
        return {"host": host, **_outcome("failed", "daemon_error", exc.message)}
    except DaemonTimeout:
        return {"host": host, **_outcome("failed", "host_timeout",
                                         "The host did not answer in time; retry to confirm.")}
    except (DaemonUnavailable, OSError) as exc:
        log.info("plugin_cli.host_unreachable host=%s error=%s", host, exc)
        return {"host": host, **_outcome("failed", "host_unreachable", "The host is unreachable.")}
    except Exception:
        log.warning("plugin_cli.host_failed host=%s", host, exc_info=True)
        return {"host": host, **_outcome("failed", "error_internal", "Unexpected error on this host.")}
    if not isinstance(res, dict) or res.get("outcome") not in ("ok", "noop", "failed"):
        return {"host": host, **_outcome("failed", "unparseable_result", "Malformed host reply.")}
    return {"host": host, **res}


def unreachable(host: str) -> dict[str, Any]:
    """The outcome for a host the fleet registry already knows is down (not asked)."""
    return {"host": host, **_outcome("failed", "host_unreachable", "The host is unreachable.")}
