"""D-16c curated plugin marketplace: policy (D1), the CLI wrapper (single writer), the
every-host fan-out (D2), the routes, the kill switch and the human gate.

Review Focus 1-4 (constraints.md) each have a block below. The CLI is faked at
``plugin_cli._run`` (its subprocess seam) or, once, by a real executable script; the real
binary is exercised by ``test_plugin_cli_contract.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.ipc.client import DaemonError, DaemonTimeout, DaemonUnavailable
from server.library import plugin_cli, plugin_policy, scan
from server.library.plugin_policy import InstallCatalog, PolicyRefusal

DIR = plugin_policy.DIRECTORY_MARKETPLACE


# =========================================================================== policy (D1)

@pytest.mark.parametrize("source", [
    "pbakaus/impeccable", "obra/superpowers", "a/b.c-d_e",
    "https://github.com/obra/superpowers.git", "https://gitlab.com/group/sub/repo",
    "anthropics/claude-plugins-official",
])
def test_marketplace_source_accepts_github_and_https(source: str) -> None:
    assert plugin_policy.check_marketplace_source(source) == source


@pytest.mark.parametrize("source", [
    "", " a/b", "a/b ", "/tmp/mkt", "./mkt", "../mkt", "~/mkt", "file:///tmp/mkt",
    "http://github.com/a/b", "git@github.com:a/b.git", "ssh://git@github.com/a/b",
    "a/b;rm -rf ~", "a/b|x", "a/$(id)", "a/`id`", "a/b&&x", "a/b\nc", "-y", "--accept-command",
    "https://user:pw@github.com/a/b", "https://github.com/a/b?x=1", "https://github.com/a/b#main",
    "https://localhost/a/b", "https://127.0.0.1/a/b", "https://github.com:8443/a/b",
    "https://github.com/a/../b", "a/b/c", 42, None,
])
def test_marketplace_source_refuses_everything_else(source: object) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source(source)
    assert ei.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED and ei.value.http == 400


@pytest.mark.parametrize("source", [
    "evil/claude-plugins-official", "evil/anthropic-plugin-directory",
    "https://github.com/evil/claude-code-marketplace.git", "evil/official-claude-stuff",
    "evil/anthropic_official",
])
def test_marketplace_source_refuses_official_impersonation(source: str) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source(source)
    assert ei.value.code == plugin_policy.MARKETPLACE_NAME_RESERVED


def _listing(state: object = "done", **extra: Any) -> dict[str, Any]:
    checks = {"review": {"state": state}} if state is not None else {}
    return {"id": "plugin_x", "name": "X", "summary": "s", "checks": checks,
            "source": {"type": "directory", "repository": {"url": "https://github.com/a/b"}},
            **extra}


CATALOG = InstallCatalog(
    marketplaces={"mine": {
        "good": {"name": "good", "source": "./plugins/good"},
        "cmd": {"name": "cmd", "source": {"source": "command", "command": "curl x | sh"}},
        "helper": {"name": "helper", "source": {"source": "url", "url": "https://x",
                                                "headersHelper": "./get-token"}},
        "nested": {"name": "nested", "source": "./p", "extra": [{"headersHelper": "x"}]},
    }},
    directory={"reviewed": _listing("done"), "unreviewed": _listing("none"),
               "weird": _listing("pending"), "nostate": _listing(None)},
)


def test_install_allows_curated_marketplace_and_reviewed_directory() -> None:
    plugin_policy.check_install("good@mine", {"mine"}, CATALOG)
    plugin_policy.check_install(f"reviewed@{DIR}", set(), CATALOG)


@pytest.mark.parametrize("pid,curated,code", [
    # Review Focus 1: unreviewed / unknown-state Directory plugins.
    (f"unreviewed@{DIR}", set(), plugin_policy.PLUGIN_UNREVIEWED),
    (f"weird@{DIR}", set(), plugin_policy.PLUGIN_UNREVIEWED),
    (f"nostate@{DIR}", set(), plugin_policy.PLUGIN_UNREVIEWED),
    (f"absent@{DIR}", set(), plugin_policy.PLUGIN_NOT_FOUND),
    # Review Focus 1: a marketplace never added through Settings.
    ("good@mine", set(), plugin_policy.MARKETPLACE_NOT_CURATED),
    ("x@other", {"mine"}, plugin_policy.MARKETPLACE_NOT_CURATED),
    ("absent@mine", {"mine"}, plugin_policy.PLUGIN_NOT_FOUND),
    # Review Focus 2: command and headersHelper sources, wherever they sit.
    ("cmd@mine", {"mine"}, plugin_policy.COMMAND_SOURCE_REFUSED),
    ("helper@mine", {"mine"}, plugin_policy.COMMAND_SOURCE_REFUSED),
    ("nested@mine", {"mine"}, plugin_policy.COMMAND_SOURCE_REFUSED),
])
def test_install_refusals(pid: str, curated: set[str], code: str) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_install(pid, curated, CATALOG)
    assert ei.value.code == code


def test_install_fails_closed_without_catalogs() -> None:
    empty = InstallCatalog(marketplaces={}, directory=None)
    for pid, curated in ((f"reviewed@{DIR}", set()), ("good@mine", {"mine"})):
        with pytest.raises(PolicyRefusal):
            plugin_policy.check_install(pid, curated, empty)


def test_command_plugin_refusal_is_409() -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_install("cmd@mine", {"mine"}, CATALOG)
    assert ei.value.http == 409 and ei.value.code == "PLUGIN_COMMAND_SOURCE_REFUSED"


@pytest.mark.parametrize("name,cfg,want", [
    ("claude-plugins-official", {}, True),
    ("knowledge-work-plugins", {}, False),
    ("first-party-plugins", {}, False),
    ("impeccable", {}, False),
    ("impeccable", {"autoUpdate": True}, True),
    ("claude-plugins-official", {"autoUpdate": False}, False),
    ("x", {"source": {"source": "claudeai"}}, True),
    ("x", {"source": {"source": "pluginDirectory"}}, True),
    (DIR, None, True),
])
def test_auto_update_mirrors_the_cli_rule(name: str, cfg: dict | None, want: bool) -> None:
    assert plugin_policy.auto_updates(name, cfg) is want


# =========================================================================== CLI wrapper

ARGS = {
    "marketplace_add": {"source": "a/b"}, "marketplace_update": {"name": "mine"},
    "marketplace_remove": {"name": "mine"}, "install": {"plugin_id": "p@m"},
    "update": {"plugin_id": "p@m"}, "uninstall": {"plugin_id": "p@m"},
    "enable": {"plugin_id": "p@m"}, "disable": {"plugin_id": "p@m"},
}
NEVER = {"-y", "--yes", "--accept-command", "--sparse", "--claudeai", "--marketplace", "--prune"}


def test_ops_table_is_complete() -> None:
    assert set(ARGS) == plugin_cli.OPS


@pytest.mark.parametrize("op", sorted(ARGS))
def test_argv_never_carries_a_consent_bypass_and_is_user_scoped(op: str) -> None:
    """Review Focus 2: -y / --accept-command / --sparse / --claudeai never appear."""
    argv = plugin_cli.build_argv("/bin/claude", op, ARGS[op])
    assert not NEVER & set(argv)
    assert argv[:2] == ["/bin/claude", "plugin"] and argv[-1] == "--json"
    if op != "marketplace_update":  # `marketplace update` takes no --scope
        assert argv[argv.index("--scope") + 1] == "user"
    if op == "uninstall":
        assert "--keep-data" in argv


@pytest.mark.parametrize("op,args", [
    ("install", {"plugin_id": "-y"}), ("install", {"plugin_id": "p@m --accept-command x"}),
    ("install", {"plugin_id": "../../x@m"}), ("install", {}), ("marketplace_add", {"source": "/tmp/x"}),
    ("marketplace_add", {"source": "--sparse"}), ("marketplace_remove", {"name": "-a"}),
    ("nope", {"plugin_id": "p@m"}), ("install", {"plugin_id": 3}),
])
def test_argv_refuses_crafted_args(op: str, args: dict) -> None:
    with pytest.raises(plugin_cli.InvalidPluginOp):
        plugin_cli.build_argv("/bin/claude", op, args)


@pytest.mark.parametrize("flag", sorted(NEVER))
def test_assert_safe_argv_blocks_every_forbidden_flag(flag: str) -> None:
    with pytest.raises(plugin_cli.InvalidPluginOp):
        plugin_cli.assert_safe_argv(["/bin/claude", "plugin", "install", "p@m", flag, "--json"])
    with pytest.raises(plugin_cli.InvalidPluginOp):
        plugin_cli.assert_safe_argv(["/bin/claude", "plugin", "install", "p@m", f"{flag}=x"])


N = "nonce123"


def _line(**kw: Any) -> str:
    return json.dumps({"nonce": N, **kw})


@pytest.mark.parametrize("op,rc,stdout,want", [
    ("install", 0, _line(outcome="ok", installedVersion="1.2.0"), ("ok", None, "1.2.0")),
    ("update", 0, _line(outcome="ok", updateOutcome="up_to_date", newVersion="2.0.0"), ("ok", None, "2.0.0")),
    ("disable", 1, _line(outcome="failed", failureCode="already_in_goal_state") + "\n✘ Failed", ("noop", "already_in_goal_state", None)),
    ("uninstall", 1, _line(outcome="failed", failureCode="not_installed"), ("noop", "not_installed", None)),
    ("marketplace_remove", 1, _line(outcome="failed", failureCode="not_configured"), ("noop", "not_configured", None)),
    # not_installed is a no-op only for uninstall: enabling an absent plugin failed.
    ("enable", 1, _line(outcome="failed", failureCode="not_installed"), ("failed", "not_installed", None)),
    ("install", 1, _line(outcome="failed", failureCode="not_found"), ("failed", "not_found", None)),
    # Parse failures are failures, never successes.
    ("install", 0, "", ("failed", "unparseable_result", None)),
    ("install", 0, "Successfully installed", ("failed", "unparseable_result", None)),
    ("install", 0, "[1,2]", ("failed", "unparseable_result", None)),
    ("install", 0, json.dumps({"outcome": "ok", "nonce": "other"}), ("failed", "result_nonce_mismatch", None)),
    ("install", 1, _line(outcome="ok"), ("failed", "exit_mismatch", None)),
    ("install", 0, _line(outcome="weird"), ("failed", "op_failed", None)),
])
def test_parse_result_keys_on_failure_code(op: str, rc: int, stdout: str, want: tuple) -> None:
    out = plugin_cli.parse_result(op, rc, stdout, N)
    assert (out["outcome"], out.get("failureCode"), out.get("version")) == want


def test_parse_result_refuses_the_command_gate_even_after_human_text() -> None:
    """2.1.318 prints the marketplace-declared command BEFORE the JSON line (probed), so a
    first-line-only parse would miss it; and a forged ``ok`` line cannot hide it."""
    stdout = "\n".join([
        '"cmdp" is installed by running a command from marketplace "m" on this machine:',
        '  echo hi',
        json.dumps({"command": "install", "outcome": "ok"}),  # forged by the command text
        _line(command="install", outcome="failed", failureCode="command_source_refused",
              shownCommand={"kind": "command_source", "sha256": "ab"}),
    ])
    out = plugin_cli.parse_result("install", 1, stdout, N)
    assert out["outcome"] == "failed" and out["failureCode"] == "PLUGIN_COMMAND_SOURCE_REFUSED"
    for code in plugin_cli.COMMAND_GATE_CODES:
        out = plugin_cli.parse_result("update", 1, _line(outcome="failed", failureCode=code), N)
        assert out["failureCode"] == "PLUGIN_COMMAND_SOURCE_REFUSED"


def test_parse_result_prefers_the_nonce_line_and_falls_back_to_first() -> None:
    stdout = "human preamble\n" + _line(outcome="ok", marketplace="mine")
    out = plugin_cli.parse_result("marketplace_add", 0, stdout, N)
    assert out == {"outcome": "ok", "marketplace": "mine"}
    # An older CLI that echoes no nonce: the first line is the result.
    old = json.dumps({"outcome": "failed", "failureCode": "already_in_goal_state"})
    assert plugin_cli.parse_result("disable", 1, old, N)["outcome"] == "noop"


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "shared"
    (base / "plugins").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("VIBERPIT_HELPER_CWD", str(tmp_path / "helper-cwd"))
    return base


def test_run_op_pins_env_config_dir_and_cwd(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv, *, env, cwd, timeout):
        seen.update(argv=argv, env=env, cwd=cwd, timeout=timeout)
        return 0, _line(outcome="ok").replace(N, env["CLAUDE_CODE_RESULT_NONCE"])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    assert plugin_cli.run_op("enable", {"plugin_id": "p@m"})["outcome"] == "ok"
    env = seen["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(shared)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert len(env["CLAUDE_CODE_RESULT_NONCE"]) == 32 and env["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["cwd"].endswith("helper-cwd") and seen["timeout"] == plugin_cli.CLI_TIMEOUT_S


def test_run_op_reads_back_the_installed_version(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"p@m": [{"version": "3.1.0", "lastUpdated": "z"}]}}))
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", lambda argv, *, env, cwd, timeout: (
        0, json.dumps({"outcome": "ok", "nonce": env["CLAUDE_CODE_RESULT_NONCE"]})))
    assert plugin_cli.run_op("install", {"plugin_id": "p@m"})["version"] == "3.1.0"


@pytest.mark.parametrize("exc,code", [
    (subprocess.TimeoutExpired(["x"], 1), "cli_timeout"), (FileNotFoundError("x"), "cli_spawn_failed")])
def test_run_op_maps_subprocess_failures(shared: Path, monkeypatch: pytest.MonkeyPatch,
                                         exc: Exception, code: str) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", boom)
    assert plugin_cli.run_op("enable", {"plugin_id": "p@m"})["failureCode"] == code


def test_run_op_without_a_binary_or_with_bad_args_never_spawns(shared: Path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_cli, "_run", lambda *a, **k: pytest.fail("spawned"))
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: None)
    assert plugin_cli.run_op("enable", {"plugin_id": "p@m"})["failureCode"] == "claude_not_found"
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    assert plugin_cli.run_op("install", {"plugin_id": "-y"})["failureCode"] == "invalid_args"


def test_run_op_against_a_fake_claude_executable(shared: Path, tmp_path: Path, monkeypatch) -> None:
    """The real subprocess seam with a fake ``claude`` script: argv list, no shell, the
    pinned env reaches the child, and the first line is what is parsed."""
    record = tmp_path / "argv.json"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
        "'cfg': os.environ.get('CLAUDE_CONFIG_DIR'), 'key': os.environ.get('ANTHROPIC_API_KEY')}))\n"
        "print(json.dumps({'command': 'disable', 'outcome': 'failed', 'failureCode': "
        "'already_in_goal_state', 'nonce': os.environ['CLAUDE_CODE_RESULT_NONCE']}))\n"
        "print('x Failed to disable')\n"
        "sys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: str(script))
    out = plugin_cli.run_op("disable", {"plugin_id": "p@m; rm -rf /"})
    assert out["failureCode"] == "invalid_args" and not record.exists()
    out = plugin_cli.run_op("disable", {"plugin_id": "p@m"})
    assert out == {"outcome": "noop", "failureCode": "already_in_goal_state"}
    seen = json.loads(record.read_text())
    assert seen == {"argv": ["plugin", "disable", "p@m", "--scope", "user", "--json"],
                    "cfg": str(shared), "key": None}


def test_refresh_available_is_ttl_gated(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_k: Any) -> tuple[int, str]:
        calls.append(argv)
        return 0, "{}"

    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    monkeypatch.setattr(plugin_cli, "_last_available_refresh", 0.0)
    assert plugin_cli.refresh_available() is True
    assert plugin_cli.refresh_available() is False
    assert calls == [["/fake/claude", "plugin", "list", "--json", "--available"]]


# =========================================================================== fan-out (D2)

class _HostOrch:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    async def plugin_op(self, op: str, args: dict) -> dict:
        self.calls.append((op, args))
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour() if callable(self.behaviour) else self.behaviour


class _Router:
    def __init__(self, hosts: dict[str, _HostOrch], reachable: list[str]) -> None:
        self.hosts, self.reachable = hosts, reachable

    def names(self) -> list[str]:
        return list(self.hosts)

    def get(self, name: str) -> _HostOrch:
        return self.hosts[name]

    async def reachable_names(self, *, always: str | None = None) -> list[str]:
        return self.reachable


def test_fleet_fan_out_reports_every_host_and_isolates_failures() -> None:
    """Review Focus 3: one host down → per-host outcomes; the others still apply."""
    from server.sessions.fleet_orchestrator import FleetOrchestrator
    state = {"installed": False}

    def install_once() -> dict:
        if state["installed"]:
            return {"outcome": "ok", "message": "already installed", "version": "1.0.0"}
        state["installed"] = True
        return {"outcome": "ok", "version": "1.0.0"}

    hosts = {
        "dobby": _HostOrch(install_once),
        "rocky": _HostOrch(DaemonUnavailable("connection refused")),
        "old": _HostOrch(DaemonError("UnknownMethod", "plugin_op")),
        "slow": _HostOrch(DaemonTimeout("read timed out")),
        "gone": _HostOrch({"outcome": "ok"}),
        "odd": _HostOrch(RuntimeError("bug")),
    }
    fleet = FleetOrchestrator(cast(Any, _Router(hosts, ["dobby", "rocky", "old", "slow", "odd"])), "dobby")
    out = asyncio.run(fleet.plugin_op_everywhere("install", {"plugin_id": "p@m"}))
    by = {h["host"]: (h["outcome"], h.get("failureCode")) for h in out}
    assert by == {"dobby": ("ok", None), "rocky": ("failed", "host_unreachable"),
                  "old": ("failed", "daemon_outdated"), "slow": ("failed", "host_timeout"),
                  "gone": ("failed", "host_unreachable"), "odd": ("failed", "error_internal")}
    assert hosts["gone"].calls == []  # known-down host is reported, not dialled
    assert hosts["dobby"].calls == [("install", {"plugin_id": "p@m"})]
    # Retrying is idempotent: the host that applied answers ok again, nothing rolls back.
    again = asyncio.run(fleet.plugin_op_everywhere("install", {"plugin_id": "p@m"}))
    assert next(h for h in again if h["host"] == "dobby")["outcome"] == "ok"


def test_host_outcome_rejects_a_malformed_reply() -> None:
    async def reply() -> Any:
        return {"outcome": "great"}
    out = asyncio.run(plugin_cli.host_outcome("h", reply()))
    assert out == {"host": "h", "outcome": "failed", "failureCode": "unparseable_result",
                   "message": "Malformed host reply."}


def test_in_process_facade_and_remote_run_on_one_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.sessions.facade import InProcessOrchestratorAdapter
    from server.sessions.remote import RemoteOrchestrator

    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    inner = _HostOrch({"outcome": "noop", "failureCode": "already_in_goal_state"})
    out = asyncio.run(InProcessOrchestratorAdapter(inner).plugin_op_everywhere("disable", {"plugin_id": "p@m"}))
    assert out == [{"host": "dobby", "outcome": "noop", "failureCode": "already_in_goal_state"}]

    class _Client:
        async def call(self, method: str, **params: Any) -> Any:
            assert method == "plugin_op" and params["timeout"] == plugin_cli.RPC_TIMEOUT_S
            raise DaemonError("UnknownMethod", method)

    remote = RemoteOrchestrator.__new__(RemoteOrchestrator)
    remote._client = _Client()
    out = asyncio.run(remote.plugin_op_everywhere("enable", {"plugin_id": "p@m"}))
    assert out[0]["host"] == "dobby" and out[0]["failureCode"] == "daemon_outdated"


def test_plugin_op_rpc_runs_the_wrapper_on_the_daemon(shared: Path, monkeypatch) -> None:
    from unittest import mock
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.sessions.orchestrator import SessionOrchestrator

    seen: list[tuple[str, dict]] = []

    def fake_run_op(op: str, args: dict) -> dict:
        seen.append((op, args))
        return {"outcome": "ok"}

    monkeypatch.setattr(plugin_cli, "run_op", fake_run_op)
    app = create_daemon_app()
    register_orchestrator_rpc(app, SessionOrchestrator(
        sdk_factory=mock.MagicMock(), poll_interval=9999.0, usage_fn=lambda n: None))
    with TestClient(app) as c:
        r = c.post("/rpc/plugin_op", json={"op": "install", "args": {"plugin_id": "p@m"}})
    assert r.json() == {"result": {"outcome": "ok"}}
    assert seen == [("install", {"plugin_id": "p@m"})]


# =========================================================================== single writer

def test_enable_disable_has_exactly_one_writer() -> None:
    """Review Focus 4: nothing in server/ or src/ writes ``enabledPlugins`` any more —
    only the CLI (through plugin_cli) does. Reads stay in scan.py."""
    root = Path(__file__).resolve().parents[2]
    writes = re.compile(r"""\[\s*["']enabledPlugins["']\s*\]\s*(?:\[[^\]]*\]\s*)?=(?!=)|"""
                        r"""setdefault\(\s*["']enabledPlugins["']""")
    hits = [str(p.relative_to(root)) for d in ("server", "src") for p in (root / d).rglob("*.py")
            if "tests" not in p.parts and writes.search(p.read_text())]
    assert hits == []
    from server.library import mutate
    assert not hasattr(mutate, "set_plugin_enabled")


# =========================================================================== routes

_H = {"Tailscale-User-Login": "owner@example.com"}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": "", "VIBERPIT_SESSION_SECRET": "x" * 32}


class _Fleet:
    """app.state.sessions stand-in: records every fan-out."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reply: list[dict] = [{"host": "dobby", "outcome": "ok"}]

    async def plugin_op_everywhere(self, op: str, args: dict) -> list[dict]:
        self.calls.append((op, args))
        return [dict(h) for h in self.reply]


def _write_marketplace(base: Path, name: str, plugins: list[dict], **cfg: Any) -> None:
    loc = base / "plugins" / "marketplaces" / name
    (loc / ".claude-plugin").mkdir(parents=True)
    (loc / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": name, "plugins": plugins}))
    km = base / "plugins" / "known_marketplaces.json"
    known = json.loads(km.read_text()) if km.exists() else {}
    known[name] = {"source": {"source": "github", "repo": f"o/{name}"}, "installLocation": str(loc), **cfg}
    km.write_text(json.dumps(known))


def _write_directory(base: Path, listings: dict[str, dict]) -> None:
    (base / "plugins" / "plugin-directory-cache-v2.json").write_text(json.dumps({
        "schemaVersion": 2,
        "listings": [{**v, "id": f"id_{k}"} for k, v in listings.items()],
        "entryNames": {f"id_{k}": k for k in listings},
    }))


@pytest.fixture()
def env(shared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    _write_marketplace(shared, "mine", [
        {"name": "good", "description": "d" * 900, "version": "1.0.0", "source": "./good"},
        {"name": "cmd", "source": {"source": "command", "command": "curl x | sh"}},
    ])
    _write_directory(shared, {
        "reviewed": {**_listing("done"), "name": "Reviewed", "release": {"version": "2.0"}},
        "unreviewed": _listing("none"),
    })
    monkeypatch.setattr(plugin_cli, "refresh_available", lambda **k: False)
    fleet = _Fleet()
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
        app.state.sessions = fleet
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            yield client, fleet, shared


async def _audit() -> list[tuple]:
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        return [(r.type, r.identity, r.detail) for r in (await db.execute(select(AuditEvent))).scalars()]


def test_install_happy_path_fans_out_and_audits(env) -> None:
    client, fleet, _ = env
    fleet.reply = [{"host": "dobby", "outcome": "ok", "version": "1.0.0"},
                   {"host": "rocky", "outcome": "failed", "failureCode": "host_unreachable"}]
    r = client.post("/api/library/plugins/good@mine/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    assert r.json() == {"op": "install", "ok": False, "hosts": fleet.reply}
    r = client.post(f"/api/library/plugins/reviewed@{DIR}/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    assert fleet.calls == [("install", {"plugin_id": "good@mine"}),
                           ("install", {"plugin_id": f"reviewed@{DIR}"})]
    events = [e for e in client.portal.call(_audit) if e[0] == "library_plugin_installed"]
    assert events[0][1] == "owner@example.com"
    assert events[0][2] == {"pluginId": "good@mine", "hosts": fleet.reply}


@pytest.mark.parametrize("pid,status,code", [
    (f"unreviewed@{DIR}", 409, "PLUGIN_UNREVIEWED"),
    (f"absent@{DIR}", 404, "PLUGIN_NOT_FOUND"),
    ("good@handadded", 409, "PLUGIN_MARKETPLACE_NOT_CURATED"),
    ("cmd@mine", 409, "PLUGIN_COMMAND_SOURCE_REFUSED"),
])
@pytest.mark.parametrize("verb", ["install", "update"])
def test_crafted_installs_are_refused_server_side(env, pid: str, status: int, code: str, verb: str) -> None:
    """Review Focus 1+2 through the route: nothing reaches any host."""
    client, fleet, shared = env
    client.get("/api/library/plugins", headers=_H)  # seeds the curated set: {"mine"}
    # A marketplace added outside ViberPit AFTER seeding is not curated.
    _write_marketplace(shared, "handadded", [{"name": "good", "source": "./g"}])
    r = client.post(f"/api/library/plugins/{pid}/{verb}", json={"confirm": True}, headers=_H)
    assert r.status_code == status and r.json()["detail"]["code"] == code
    assert fleet.calls == []


def test_install_requires_confirm(env) -> None:
    client, fleet, _ = env
    for body in ({}, {"confirm": False}):
        r = client.post("/api/library/plugins/good@mine/install", json=body, headers=_H)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_CONFIRM_REQUIRED"
    assert fleet.calls == []


MUTATIONS = [
    ("post", "/api/library/plugins/good@mine/install", {"confirm": True}),
    ("post", "/api/library/plugins/good@mine/update", {"confirm": True}),
    ("delete", "/api/library/plugins/good@mine", None),
    ("patch", "/api/library/plugins/good@mine", {"enabled": True}),
    ("post", "/api/library/marketplaces", {"source": "o/r", "confirm": True}),
    ("post", "/api/library/marketplaces/mine/update", None),
    ("delete", "/api/library/marketplaces/mine", None),
]


def _send(client: TestClient, method: str, url: str, body: dict | None):
    return client.request(method.upper(), url, headers=_H, **({"json": body} if body is not None else {}))


@pytest.mark.parametrize("method,url,body", MUTATIONS)
@pytest.mark.parametrize("via", ["assistant", "local"])
def test_every_mutation_is_human_only(env, monkeypatch, method: str, url: str, body, via: str) -> None:
    client, fleet, _ = env
    from server.api import library
    from server.auth import identity
    if via == "local":
        monkeypatch.setattr(identity, "attested_front_end_hop", lambda _c: False)
    else:
        monkeypatch.setattr(library, "human_gate_via", lambda _r: "assistant")
    r = _send(client, method, url, body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert fleet.calls == []


@pytest.mark.parametrize("method,url,body,blocked", [
    (*MUTATIONS[0], True), (*MUTATIONS[1], True), (*MUTATIONS[2], False), (*MUTATIONS[3], False),
    (*MUTATIONS[4], True), (*MUTATIONS[5], True), (*MUTATIONS[6], False),
])
def test_kill_switch(env, monkeypatch, method: str, url: str, body, blocked: bool) -> None:
    client, fleet, shared = env
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"good@mine": [{"version": "1.0.0"}]}}))
    monkeypatch.setenv("VIBERPIT_PLUGIN_INSTALL_ENABLED", "false")
    r = _send(client, method, url, body)
    if blocked:
        assert r.status_code == 404 and r.json() == {"error": "plugin_install_disabled"}
        assert fleet.calls == []
    else:
        assert r.status_code == 200 and len(fleet.calls) == 1
    browse = client.get("/api/library/plugins/browse", headers=_H).json()
    assert browse["plugins"] == [] and browse["installEnabled"] is False
    assert client.get("/api/library/plugins", headers=_H).json()["installEnabled"] is False


def test_browse_lists_curated_and_reviewed_only(env) -> None:
    client, _, shared = env
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"good@mine": [{"version": "1.0.0"}]}}))
    body = client.get("/api/library/plugins/browse", headers=_H).json()
    ids = [p["pluginId"] for p in body["plugins"]]
    assert ids == ["good@mine", f"reviewed@{DIR}"]  # no cmd@mine, no unreviewed
    assert body["total"] == 2 and body["installEnabled"] is True
    good, reviewed = body["plugins"]
    assert len(good["description"]) == 500 and good["installed"] is True and good["reviewed"] is False
    assert good["components"] is None and good["version"] == "1.0.0"
    assert reviewed == {"pluginId": f"reviewed@{DIR}", "name": "Reviewed", "description": "s",
                        "marketplace": DIR, "version": "2.0", "reviewed": True,
                        "components": None, "installed": False}
    assert "unreviewed" not in json.dumps(body)


def test_browse_search_and_pagination(env) -> None:
    client, _, _ = env
    page = client.get("/api/library/plugins/browse?q=REVIEW&limit=1", headers=_H).json()
    assert [p["pluginId"] for p in page["plugins"]] == [f"reviewed@{DIR}"] and page["total"] == 1
    page = client.get("/api/library/plugins/browse?marketplace=mine&offset=0&limit=100", headers=_H).json()
    assert [p["pluginId"] for p in page["plugins"]] == ["good@mine"]
    page = client.get("/api/library/plugins/browse?offset=1&limit=1", headers=_H).json()
    assert page["offset"] == 1 and len(page["plugins"]) == 1 and page["total"] == 2
    for bad in ("limit=101", "limit=0", "offset=-1"):
        assert client.get(f"/api/library/plugins/browse?{bad}", headers=_H).status_code == 422


def test_marketplace_add_validates_curates_and_audits(env) -> None:
    client, fleet, _ = env
    r = client.post("/api/library/marketplaces", json={"source": "/tmp/local", "confirm": True}, headers=_H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_MARKETPLACE_SOURCE_REFUSED"
    r = client.post("/api/library/marketplaces", json={"source": "o/newmkt"}, headers=_H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_CONFIRM_REQUIRED"
    assert fleet.calls == []
    fleet.reply = [{"host": "dobby", "outcome": "ok", "marketplace": "newmkt"},
                   {"host": "rocky", "outcome": "failed", "failureCode": "daemon_outdated"}]
    r = client.post("/api/library/marketplaces", json={"source": "o/newmkt", "confirm": True}, headers=_H)
    assert r.status_code == 200 and r.json()["marketplaces"] == ["newmkt"]
    assert fleet.calls == [("marketplace_add", {"source": "o/newmkt"})]
    # Now curated: its update route is allowed; removing it un-curates it.
    assert client.post("/api/library/marketplaces/newmkt/update", headers=_H).status_code == 200
    assert client.delete("/api/library/marketplaces/newmkt", headers=_H).status_code == 200
    r = client.post("/api/library/marketplaces/newmkt/update", headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLUGIN_MARKETPLACE_NOT_CURATED"
    types = [e[0] for e in client.portal.call(_audit)]
    assert types.count("library_marketplace_added") == 1
    assert "library_marketplace_updated" in types and "library_marketplace_removed" in types
    added = next(e for e in client.portal.call(_audit) if e[0] == "library_marketplace_added")
    assert added[2]["source"] == "o/newmkt" and added[2]["hosts"] == fleet.reply


def test_uninstall_keeps_data_and_toggle_reaches_every_host(env) -> None:
    client, fleet, shared = env
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"good@mine": [{"version": "1.0.0"}]}}))
    assert client.patch("/api/library/plugins/good@mine", json={"enabled": False}, headers=_H).json()["op"] == "disable"
    assert client.patch("/api/library/plugins/nope@mine", json={"enabled": True}, headers=_H).status_code == 404
    assert client.delete("/api/library/plugins/good@mine", headers=_H).json()["op"] == "uninstall"
    assert fleet.calls == [("disable", {"plugin_id": "good@mine"}), ("uninstall", {"plugin_id": "good@mine"})]
    types = [e[0] for e in client.portal.call(_audit)]
    assert {"library_plugin_toggled", "library_plugin_uninstalled"} <= set(types)


def test_plugins_list_carries_version_auto_update_source_and_curation(env) -> None:
    client, _, shared = env
    _write_marketplace(shared, "claude-plugins-official", [])
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "good@mine": [{"version": "1.0.0"}], "x@claude-plugins-official": [{"version": "9"}]}}))
    body = client.get("/api/library/plugins", headers=_H).json()
    rows = {p["id"]: p for p in body["plugins"]}
    assert (rows["good@mine"]["version"], rows["good@mine"]["autoUpdate"]) == ("1.0.0", False)
    assert rows["good@mine"]["source"] == {"source": "github", "repo": "o/mine"}
    assert rows["x@claude-plugins-official"]["autoUpdate"] is True
    assert {m["name"]: m["curated"] for m in body["marketplaces"]} == {
        "mine": True, "claude-plugins-official": True}  # both configured at first read
    assert body["installEnabled"] is True


def test_dead_marketplace_route_is_gone(env) -> None:
    client, _, _ = env
    assert client.get("/api/library/marketplace", headers=_H).status_code in (404, 405)


def test_settings_lists_the_kill_switch() -> None:
    from server.api.settings import _MANAGED
    assert ("plugin_install_enabled", "VIBERPIT_PLUGIN_INSTALL_ENABLED") in {r[:2] for r in _MANAGED}


def test_browse_entries_never_include_unreviewed(shared: Path) -> None:
    _write_directory(shared, {f"p{i}": _listing("none" if i % 2 else "done") for i in range(6)})
    out = scan.browse_entries(set())
    assert [e["pluginId"] for e in out] == [f"p{i}@{DIR}" for i in (0, 2, 4)]
