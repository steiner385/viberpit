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
from server.library import directory_api, plugin_cli, plugin_policy, scan
from server.library.plugin_policy import InstallCatalog, PolicyRefusal

DIR = plugin_policy.DIRECTORY_MARKETPLACE


# =========================================================================== policy (D1)

PUBLIC_IP = "140.82.112.3"


@pytest.fixture(autouse=True)
def _dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """No real DNS in unit tests: every name resolves public unless a test says otherwise."""
    table: dict[str, list[str]] = {}
    monkeypatch.setattr(plugin_policy, "_resolve", lambda host: table.get(host, [PUBLIC_IP]))
    return table


@pytest.mark.parametrize("source", [
    "pbakaus/impeccable", "obra/superpowers", "a/b.c-d_e", "MixedCase/Repo",
    "https://github.com/obra/superpowers.git", "https://gitlab.com/group/sub/repo",
    "anthropics/claude-plugins-official",
])
def test_marketplace_source_accepts_github_and_https(source: str) -> None:
    assert plugin_policy.check_marketplace_source(source) == source


@pytest.mark.parametrize("source", [
    "", " a/b", "a/b ", "/tmp/mkt", "./mkt", "../mkt", "~/mkt", "file:///tmp/mkt",
    "http://github.com/a/b", "git@github.com:a/b.git", "ssh://git@github.com/a/b",
    "a/b;rm -rf ~", "a/b|x", "a/$(id)", "a/`id`", "a/b&&x", "a/b\nc", "a/b\n", "a/b%0A",
    "-y", "--accept-command",
    "https://user:pw@github.com/a/b", "https://github.com/a/b?x=1", "https://github.com/a/b#main",
    "https://github.com:8443/a/b", "https://github.com/a/../b", "a/b/c", 42, None,
])
def test_marketplace_source_refuses_everything_else(source: object) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source(source)
    assert ei.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED and ei.value.http == 400


@pytest.mark.parametrize("host", [
    # Every IP-literal spelling inet_aton accepts, and localhost names.
    "127.0.0.1", "127.1", "0x7f.1", "0x7f.0.0.1", "017700000001", "2130706433", "0177.0.0.1",
    "10.0.0.1", "0.0.0.0", "169.254.169.254", "localhost", "foo.localhost", "a.b.localhost",
])
def test_marketplace_source_refuses_ip_literals_and_localhost(host: str, _dns) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source(f"https://{host}/a/b")
    assert ei.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED


@pytest.mark.parametrize("addrs", [
    ["127.0.0.1"], ["10.1.2.3"], ["192.168.1.5"], ["172.16.0.9"], ["169.254.0.1"],
    ["100.100.1.2"], ["100.64.0.1"],  # CGNAT: the tailnet
    ["224.0.0.1"], ["0.0.0.0"], ["::1"], ["fd00::1"], ["fe80::1%eth0"], ["::ffff:10.0.0.1"],
    [PUBLIC_IP, "10.0.0.1"],  # any internal answer refuses
    [],  # does not resolve
])
def test_marketplace_source_refuses_hosts_resolving_internal(addrs: list[str], _dns) -> None:
    _dns["git.example.com"] = addrs
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source("https://git.example.com/a/b")
    assert ei.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED


def test_marketplace_source_resolution_failure_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(host: str) -> list[str]:
        raise OSError("NXDOMAIN")
    monkeypatch.setattr(plugin_policy, "_resolve", boom)
    with pytest.raises(PolicyRefusal):
        plugin_policy.check_marketplace_source("https://git.example.com/a/b")
    assert plugin_policy.check_marketplace_source("o/r") == "o/r"  # no lookup for GitHub shorthand


@pytest.mark.parametrize("source", [
    "evil/claude-plugins-official", "evil/anthropic-plugin-directory",
    "https://github.com/evil/claude-code-marketplace.git", "evil/official-claude-stuff",
    "evil/anthropic_official",
])
def test_marketplace_source_refuses_official_impersonation(source: str) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source(source)
    assert ei.value.code == plugin_policy.MARKETPLACE_NAME_RESERVED


@pytest.mark.parametrize("given,want", [
    # The owner's add string and every form the CLI records for it (bCe @280765576).
    ("O/Repo", "github:o/repo"),
    ({"source": "github", "repo": "O/Repo"}, "github:o/repo"),
    ("https://github.com/O/Repo", "github:o/repo"),
    ({"source": "git", "url": "https://github.com/O/Repo.git"}, "github:o/repo"),
    ("https://gitlab.com/g/sub/r.git", "https:gitlab.com/g/sub/r"),
    ({"source": "git", "url": "https://GitLab.com/g/sub/r.git"}, "https:gitlab.com/g/sub/r"),
    ({"source": "url", "url": "https://x.example/m/"}, "https:x.example/m"),
    ({"source": "directory", "path": "/tmp/m"}, "directory:/tmp/m"),
    # Pins, other transports and unknown types never equal a recorded source.
    ({"source": "github", "repo": "o/r", "ref": "evil"}, None),
    ({"source": "git", "url": "https://github.com/o/r.git", "path": "sub"}, None),
    ({"source": "git", "url": "git@github.com:o/r.git"}, None),
    ({"source": "url", "url": "http://x.example/m"}, None),
    ({"source": "npm", "package": "x"}, None),
    ({"source": "directory", "path": "rel"}, None),
    ("/tmp/m", None), (None, None), (3, None),
])
def test_normalize_source(given: object, want: str | None) -> None:
    assert plugin_policy.normalize_source(given) == want


def test_add_form_round_trips_and_refuses_local() -> None:
    assert plugin_policy.add_form("github:o/r") == "o/r"
    assert plugin_policy.add_form("https:gitlab.com/g/r") == "https://gitlab.com/g/r"
    assert plugin_policy.add_form("directory:/tmp/m") is None and plugin_policy.add_form(None) is None


def test_marketplace_names_accept_case_and_refuse_newlines() -> None:
    assert plugin_policy.valid_marketplace_name("MixedCase-Mkt")
    for bad in ("mine\n", "mine\r", "", "-x", "a b", "a/b", 3):
        assert not plugin_policy.valid_marketplace_name(bad)


def _listing(state: object = "done", **extra: Any) -> dict[str, Any]:
    checks = {"review": {"state": state}} if state is not None else {}
    return {"id": "plugin_x", "name": "X", "summary": "s", "checks": checks,
            "source": {"type": "directory", "repository": {"url": "https://github.com/a/b"}},
            **extra}


MINE = "github:o/mine"
CURATED = {"mine": MINE}
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
    sources={"mine": MINE, "other": "github:o/other"},
)


def test_install_allows_curated_marketplace_and_reviewed_directory() -> None:
    plugin_policy.check_install("good@mine", CURATED, CATALOG)
    plugin_policy.check_install(f"reviewed@{DIR}", {}, CATALOG)


@pytest.mark.parametrize("pid,curated,code", [
    # Review Focus 1: unreviewed / unknown-state Directory plugins.
    (f"unreviewed@{DIR}", {}, plugin_policy.PLUGIN_UNREVIEWED),
    (f"weird@{DIR}", {}, plugin_policy.PLUGIN_UNREVIEWED),
    (f"nostate@{DIR}", {}, plugin_policy.PLUGIN_UNREVIEWED),
    (f"absent@{DIR}", {}, plugin_policy.PLUGIN_NOT_FOUND),
    # Review Focus 1: a marketplace never confirmed through Settings.
    ("good@mine", {}, plugin_policy.MARKETPLACE_NOT_CURATED),
    ("x@other", CURATED, plugin_policy.MARKETPLACE_NOT_CURATED),
    ("absent@mine", CURATED, plugin_policy.PLUGIN_NOT_FOUND),
    # Review Focus 2: command and headersHelper sources, wherever they sit.
    ("cmd@mine", CURATED, plugin_policy.COMMAND_SOURCE_REFUSED),
    ("helper@mine", CURATED, plugin_policy.COMMAND_SOURCE_REFUSED),
    ("nested@mine", CURATED, plugin_policy.COMMAND_SOURCE_REFUSED),
])
def test_install_refusals(pid: str, curated: dict[str, str], code: str) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_install(pid, curated, CATALOG)
    assert ei.value.code == code


@pytest.mark.parametrize("configured", ["github:evil/x", None])
def test_install_refuses_a_re_pointed_curated_name(configured: str | None) -> None:
    """Fix round 1, finding 1: ``tools`` re-added from another repo (or a source that no
    longer normalizes) is no longer curated, with a reason the UI can act on."""
    swapped = InstallCatalog(marketplaces=CATALOG.marketplaces, directory=None,
                             sources={"mine": configured})
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_install("good@mine", CURATED, swapped)
    assert (ei.value.code, ei.value.reason) == (plugin_policy.MARKETPLACE_NOT_CURATED,
                                                plugin_policy.REASON_SOURCE_CHANGED)
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_install("good@mine", {}, CATALOG)
    assert ei.value.reason == plugin_policy.REASON_NOT_CONFIRMED


def test_install_fails_closed_without_catalogs() -> None:
    empty = InstallCatalog(marketplaces={}, directory=None, sources={"mine": MINE})
    for pid, curated in ((f"reviewed@{DIR}", {}), ("good@mine", CURATED)):
        with pytest.raises(PolicyRefusal):
            plugin_policy.check_install(pid, curated, empty)


def test_command_plugin_refusal_is_409() -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_install("cmd@mine", CURATED, CATALOG)
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
    # An older CLI that echoes no nonce at all: the first line is the result.
    old = json.dumps({"outcome": "failed", "failureCode": "already_in_goal_state"})
    assert plugin_cli.parse_result("disable", 1, old, N)["outcome"] == "noop"


@pytest.mark.parametrize("stdout", [
    # Fix round 1, finding 5: once any line carries a nonce, only ours counts — a nonce-less
    # first line (e.g. forged by text the CLI echoes) is never the fallback.
    json.dumps({"outcome": "ok"}) + "\n" + json.dumps({"outcome": "failed", "nonce": "other"}),
    json.dumps({"outcome": "ok"}) + "\n" + json.dumps({"outcome": "ok", "nonce": "other"}),
    json.dumps({"outcome": "ok", "nonce": "other"}),
])
def test_parse_result_never_falls_back_when_a_nonce_is_present(stdout: str) -> None:
    out = plugin_cli.parse_result("install", 0, stdout, N)
    assert (out["outcome"], out["failureCode"]) == ("failed", "result_nonce_mismatch")


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


# --------------------------------------------------------------- DNS pin (D16c residual)

def test_resolved_pin_for_https_sources_and_none_for_github(_dns) -> None:
    _dns["git.example.com"] = ["140.82.112.5", "140.82.112.4", "140.82.112.5"]
    assert (plugin_policy.resolved_pin("https://Git.Example.com/m")
            == "git.example.com:443:140.82.112.4,140.82.112.5")
    assert plugin_policy.resolved_pin("acme/m") is None


def test_resolved_pin_refuses_what_the_check_refuses(_dns) -> None:
    _dns["evil.example.com"] = ["10.0.0.5"]
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.resolved_pin("https://evil.example.com/m")
    assert ei.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED


def test_resolved_pin_resolves_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(plugin_policy, "_resolve", lambda h: calls.append(h) or [PUBLIC_IP])
    plugin_policy.resolved_pin("https://git.example.com/m")
    assert calls == ["git.example.com"]


def test_pin_for_normalized_only_pins_https(_dns) -> None:
    _dns["git.example.com"] = ["140.82.112.4"]
    assert (plugin_policy.pin_for_normalized("https:git.example.com/m")
            == "git.example.com:443:140.82.112.4")
    assert plugin_policy.pin_for_normalized("github:o/r") is None
    assert plugin_policy.pin_for_normalized(None) is None


def test_git_pin_for_ops(_dns) -> None:
    _dns["git.example.com"] = ["140.82.112.4"]
    pin = "git.example.com:443:140.82.112.4"
    assert plugin_cli.git_pin("marketplace_add", {"source": "https://git.example.com/m"}) == pin
    assert plugin_cli.git_pin("marketplace_add", {"source": "acme/m"}) is None
    assert plugin_cli.git_pin("marketplace_update", {
        "name": "m", "expected_source": "https:git.example.com/m"}) == pin
    assert plugin_cli.git_pin("marketplace_update", {
        "name": "m", "expected_source": "github:o/r"}) is None
    assert plugin_cli.git_pin("install", {"plugin_id": "p@m"}) is None


def test_env_git_pin_writes_a_global_config_that_includes_the_users(
        shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "GIT_CONFIG_GLOBAL" not in plugin_cli._env(N)
    pin = "git.example.com:443:140.82.112.4"
    env = plugin_cli._env(N, git_pin=pin)
    path = Path(env["GIT_CONFIG_GLOBAL"])
    try:
        assert path.read_text() == plugin_cli.git_pin_config(pin) == (
            f"[include]\n\tpath = ~/.gitconfig\n[http]\n\tcurloptResolve = {pin}\n")
    finally:
        path.unlink()


def test_run_op_removes_the_pin_file_and_passes_it_to_the_cli(
        shared: Path, monkeypatch: pytest.MonkeyPatch, _dns) -> None:
    _dns["git.example.com"] = ["140.82.112.4"]
    seen: dict[str, Any] = {}

    def fake_run(argv, *, env, cwd, timeout):
        path = Path(env["GIT_CONFIG_GLOBAL"])
        seen.update(path=path, text=path.read_text())
        return 0, json.dumps({"outcome": "ok", "nonce": env["CLAUDE_CODE_RESULT_NONCE"]})

    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    out = plugin_cli.run_op("marketplace_add", {"source": "https://git.example.com/m"})
    assert out["outcome"] == "ok"
    assert "curloptResolve = git.example.com:443:140.82.112.4" in seen["text"]
    assert not seen["path"].exists()


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("git", 1), OSError("boom")])
def test_run_op_removes_the_pin_file_when_the_cli_fails_to_run(
        shared: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    seen: dict[str, Path] = {}

    def fake_run(argv, *, env, cwd, timeout):
        seen["path"] = Path(env["GIT_CONFIG_GLOBAL"])
        raise exc

    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    out = plugin_cli.run_op("marketplace_add", {"source": "https://git.example.com/m"})
    assert out["outcome"] == "failed" and not seen["path"].exists()


def test_run_op_gives_a_github_add_no_pin(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv, *, env, cwd, timeout):
        seen["env"] = env
        return 0, json.dumps({"outcome": "ok", "nonce": env["CLAUDE_CODE_RESULT_NONCE"]})

    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    plugin_cli.run_op("marketplace_add", {"source": "acme/m"})
    assert "GIT_CONFIG_GLOBAL" not in seen["env"]


def test_marketplace_update_with_an_internal_source_is_refused_before_the_cli(
        shared: Path, monkeypatch: pytest.MonkeyPatch, _dns) -> None:
    _write_marketplace(shared, "m", [], repo="o/r")
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", lambda *a, **k: pytest.fail("the CLI ran"))
    monkeypatch.setattr(plugin_cli, "host_policy", lambda op, args: None)
    _dns["git.example.com"] = ["10.0.0.5"]
    out = plugin_cli.run_op("marketplace_update", {
        "name": "m", "expected_source": "https:git.example.com/m"})
    assert (out["outcome"], out["failureCode"]) == (
        "failed", plugin_policy.MARKETPLACE_SOURCE_REFUSED)


def _ok_runner(calls: list[list[str]] | None = None):
    def fake_run(argv, *, env, cwd, timeout):
        if calls is not None:
            calls.append(argv)
        return 0, json.dumps({"outcome": "ok", "nonce": env["CLAUDE_CODE_RESULT_NONCE"]})
    return fake_run


def test_run_op_reads_back_the_installed_version(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_marketplace(shared, "m", [{"name": "p", "source": "./p"}])
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"p@m": [{"version": "3.1.0", "lastUpdated": "z"}]}}))
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", _ok_runner())
    out = plugin_cli.run_op("install", {"plugin_id": "p@m", "expected_source": "github:o/m"})
    assert out == {"outcome": "ok", "version": "3.1.0"}


@pytest.mark.parametrize("op,args,reason", [
    # Fix round 1, finding 1, on a WORKER: a session there removed `tools` and re-added
    # evil/x under the same name. The edge never sees it; this host refuses on its own.
    ("install", {"plugin_id": "foo@tools", "expected_source": "github:o/tools"}, "source_changed"),
    ("update", {"plugin_id": "foo@tools", "expected_source": "github:o/tools"}, "source_changed"),
    ("marketplace_update", {"name": "tools", "expected_source": "github:o/tools"}, "source_changed"),
    ("install", {"plugin_id": "foo@tools"}, "not_confirmed"),  # no expected source: fail closed
    ("marketplace_update", {"name": "tools"}, "not_confirmed"),
])
def test_host_refuses_a_re_pointed_marketplace_without_spawning(
        shared: Path, monkeypatch: pytest.MonkeyPatch, op: str, args: dict, reason: str) -> None:
    _write_marketplace(shared, "tools", [{"name": "foo", "source": "./foo"}], repo="evil/x")
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", lambda *a, **k: pytest.fail("the CLI ran"))
    out = plugin_cli.run_op(op, args)
    assert (out["outcome"], out["failureCode"], out["reason"]) == (
        "failed", "PLUGIN_MARKETPLACE_NOT_CURATED", reason)


def test_host_installs_when_its_source_matches(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_marketplace(shared, "tools", [{"name": "foo", "source": "./foo"},
                                         {"name": "cmd", "source": {"source": "command", "command": "x"}}])
    calls: list[list[str]] = []
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", _ok_runner(calls))
    args = {"plugin_id": "foo@tools", "expected_source": "github:o/tools"}
    assert plugin_cli.run_op("install", args)["outcome"] == "ok"
    assert plugin_cli.run_op("marketplace_update", {"name": "tools", "expected_source": "github:o/tools"})["outcome"] == "ok"
    # The command-source check runs on the host too, against ITS catalog.
    cmd = plugin_cli.run_op("install", {"plugin_id": "cmd@tools", "expected_source": "github:o/tools"})
    assert cmd["failureCode"] == "PLUGIN_COMMAND_SOURCE_REFUSED"
    assert [c[2] for c in calls] == ["install", "marketplace"]


def test_host_rechecks_directory_review_state(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_directory(shared, {"good": _listing("done"), "bad": _listing("none")})
    refreshed: list[bool] = []

    def fake_refresh(**_k: Any) -> bool:
        refreshed.append(True)
        return False

    monkeypatch.setattr(plugin_cli, "refresh_available", fake_refresh)
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", _ok_runner())
    assert plugin_cli.run_op("install", {"plugin_id": f"bad@{DIR}"})["failureCode"] == "PLUGIN_UNREVIEWED"
    assert plugin_cli.run_op("install", {"plugin_id": f"good@{DIR}"})["outcome"] == "ok"
    assert refreshed == [True, True]


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


def test_refresh_available_runs_on_a_miss_and_again_once_the_ttl_expires(
        shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 1, finding 7: refreshed on expiry, not only when the cache is missing."""
    calls: list[list[str]] = []
    clock = {"now": 10_000.0}

    def fake_run(argv: list[str], **_k: Any) -> tuple[int, str]:
        calls.append(argv)
        return 0, "{}"

    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    monkeypatch.setattr(plugin_cli, "_last_available_refresh", 0.0)
    monkeypatch.setattr(plugin_cli.time, "monotonic", lambda: clock["now"])
    assert plugin_cli.refresh_available() is True
    _write_directory(shared, {"x": _listing("done")})  # the cache now exists
    clock["now"] += plugin_cli.AVAILABLE_REFRESH_TTL_S - 1
    assert plugin_cli.refresh_available() is False
    clock["now"] += 2
    assert plugin_cli.refresh_available() is True  # expired: refreshed though the file exists
    assert calls == [["/fake/claude", "plugin", "list", "--json", "--available"]] * 2


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
        self.reload_reply: list[dict] = [
            {"host": "dobby", "sessionId": "s1", "name": "alpha", "outcome": "applied"}]

    async def plugin_op_everywhere(self, op: str, args: dict) -> list[dict]:
        self.calls.append((op, args))
        return [dict(h) for h in self.reply]

    async def reload_plugins_everywhere(self, hold_on_cache_impact: bool = True) -> list[dict]:
        self.calls.append(("reload", hold_on_cache_impact))  # type: ignore[arg-type]
        return [dict(r) for r in self.reload_reply]


def _write_marketplace(base: Path, name: str, plugins: list[dict], *, repo: str | None = None,
                       **cfg: Any) -> None:
    """Configure *name* the way ``marketplace add <repo>`` would (default ``o/<name>``)."""
    loc = base / "plugins" / "marketplaces" / name
    (loc / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (loc / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": name, "plugins": plugins}))
    km = base / "plugins" / "known_marketplaces.json"
    known = json.loads(km.read_text()) if km.exists() else {}
    known[name] = {"source": {"source": "github", "repo": repo or f"o/{name}"},
                   "installLocation": str(loc), **cfg}
    km.write_text(json.dumps(known))


def _write_directory(base: Path, listings: dict[str, dict]) -> None:
    (base / "plugins" / "plugin-directory-cache-v2.json").write_text(json.dumps({
        "schemaVersion": 2,
        "listings": [{**v, "id": f"id_{k}"} for k, v in listings.items()],
        "entryNames": {f"id_{k}": k for k in listings},
    }))


def _confirm(client: TestClient, name: str) -> Any:
    """The owner's Confirm: echo back the normalizedSource the list showed."""
    shown = next(m for m in client.get("/api/library/plugins", headers=_H).json()["marketplaces"]
                 if m["name"] == name)
    return client.post(f"/api/library/marketplaces/{name}/confirm",
                       json={"source": shown["normalizedSource"], "confirm": True}, headers=_H)


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

    async def live_echo(listing_id: str) -> dict | None:
        """Default live Directory: agrees with the cache (tests override to diverge)."""
        return next((v for v in (scan.directory_listings() or {}).values()
                     if v.get("id") == listing_id), None)

    monkeypatch.setattr(directory_api, "fetch_listing", live_echo)
    fleet = _Fleet()
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
        app.state.sessions = fleet
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            assert _confirm(client, "mine").status_code == 200
            yield client, fleet, shared


async def _audit() -> list[tuple]:
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        return [(r.type, r.identity, r.detail) for r in (await db.execute(select(AuditEvent))).scalars()]


def test_install_happy_path_fans_out_with_the_expected_source_and_audits(env) -> None:
    client, fleet, _ = env
    fleet.reply = [{"host": "dobby", "outcome": "ok", "version": "1.0.0"},
                   {"host": "rocky", "outcome": "failed", "failureCode": "host_unreachable"}]
    r = client.post("/api/library/plugins/good@mine/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    assert r.json() == {"op": "install", "ok": False, "hosts": fleet.reply}
    r = client.post(f"/api/library/plugins/reviewed@{DIR}/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    # Every host re-checks against its own state with the confirmed source.
    assert fleet.calls == [("install", {"plugin_id": "good@mine", "expected_source": "github:o/mine"}),
                           ("install", {"plugin_id": f"reviewed@{DIR}"})]
    events = [e for e in client.portal.call(_audit) if e[0] == "library_plugin_installed"]
    assert events[0][1] == "owner@example.com"
    assert events[0][2] == {"pluginId": "good@mine", "hosts": fleet.reply}


@pytest.mark.parametrize("pid,status,code", [
    (f"unreviewed@{DIR}", 409, "PLUGIN_UNREVIEWED"),
    (f"absent@{DIR}", 404, "PLUGIN_NOT_FOUND"),
    ("good@handadded", 409, "PLUGIN_MARKETPLACE_NOT_CURATED"),
    ("cmd@mine", 409, "PLUGIN_COMMAND_SOURCE_REFUSED"),
    ("good%0A@mine", 400, None), ("good@mine%0A", 400, None),
])
@pytest.mark.parametrize("verb", ["install", "update"])
def test_crafted_installs_are_refused_server_side(env, pid: str, status: int, code: str | None,
                                                  verb: str) -> None:
    """Review Focus 1+2 through the route: nothing reaches any host."""
    client, fleet, shared = env
    _write_marketplace(shared, "handadded", [{"name": "good", "source": "./g"}])  # never confirmed
    r = client.post(f"/api/library/plugins/{pid}/{verb}", json={"confirm": True}, headers=_H)
    assert r.status_code == status
    if code:
        assert r.json()["detail"]["code"] == code
    assert fleet.calls == []


def test_name_swap_on_the_edge_is_refused_until_reconfirmed(env) -> None:
    """Fix round 1, finding 1: a session removes `mine` and re-adds evil/x under the same
    name. Install, update, browse and marketplace update all refuse with reason
    source_changed; the list says so; re-confirming the NEW source is an explicit act."""
    client, fleet, shared = env
    _write_marketplace(shared, "mine", [{"name": "good", "source": "./evil"}], repo="evil/x")
    for url in ("/api/library/plugins/good@mine/install", "/api/library/plugins/good@mine/update"):
        r = client.post(url, json={"confirm": True}, headers=_H)
        assert r.status_code == 409
        assert r.json()["detail"] | {"message": ""} == {
            "code": "PLUGIN_MARKETPLACE_NOT_CURATED", "reason": "source_changed", "message": ""}
    r = client.post("/api/library/marketplaces/mine/update", headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "source_changed"
    assert [p["pluginId"] for p in client.get("/api/library/plugins/browse", headers=_H).json()["plugins"]] \
        == [f"reviewed@{DIR}"]
    shown = {m["name"]: m for m in client.get("/api/library/plugins", headers=_H).json()["marketplaces"]}["mine"]
    assert (shown["curation"], shown["confirmedSource"], shown["normalizedSource"]) == (
        "source_changed", "github:o/mine", "github:evil/x")
    assert fleet.calls == []
    # A confirm that echoes the OLD source is refused (the swap happened after display).
    r = client.post("/api/library/marketplaces/mine/confirm",
                    json={"source": "github:o/mine", "confirm": True}, headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "source_changed"
    assert _confirm(client, "mine").status_code == 200  # the owner re-confirms evil/x on purpose
    assert client.post("/api/library/plugins/good@mine/install", json={"confirm": True},
                       headers=_H).status_code == 200


def test_unconfirmed_marketplaces_are_listed_not_installable(env) -> None:
    """Fix round 1, finding 2: a marketplace configured outside ViberPit (at ship time or
    by a session) is unconfirmed — listed with its source, not browsable or installable —
    until the owner confirms it once, through the human gate, with an audit row."""
    client, fleet, shared = env
    _write_marketplace(shared, "byhand", [{"name": "tool", "source": "./t"}])
    shown = {m["name"]: m for m in client.get("/api/library/plugins", headers=_H).json()["marketplaces"]}
    assert (shown["byhand"]["curation"], shown["byhand"]["normalizedSource"],
            shown["byhand"]["confirmedSource"]) == ("unconfirmed", "github:o/byhand", None)
    assert shown["mine"]["curation"] == "confirmed"
    r = client.post("/api/library/plugins/tool@byhand/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "not_confirmed"
    assert "tool@byhand" not in json.dumps(client.get("/api/library/plugins/browse", headers=_H).json())
    r = client.post("/api/library/marketplaces/byhand/confirm",
                    json={"source": "github:o/byhand"}, headers=_H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_CONFIRM_REQUIRED"
    r = _confirm(client, "byhand")
    assert r.json() == {"name": "byhand", "confirmedSource": "github:o/byhand", "curation": "confirmed"}
    assert "tool@byhand" in json.dumps(client.get("/api/library/plugins/browse", headers=_H).json())
    assert client.post("/api/library/plugins/tool@byhand/install", json={"confirm": True},
                       headers=_H).status_code == 200
    events = [e for e in client.portal.call(_audit) if e[0] == "library_marketplace_confirmed"]
    assert events[-1][2] == {"name": "byhand", "normalizedSource": "github:o/byhand"}


@pytest.mark.parametrize("source", [
    {"source": "directory", "path": "/tmp/local"}, {"source": "github", "repo": "o/r", "ref": "x"},
    {"source": "url", "url": "http://plain.example/m"},
])
def test_confirm_refuses_sources_the_add_policy_refuses(env, source: dict) -> None:
    client, _, shared = env
    km = shared / "plugins" / "known_marketplaces.json"
    known = json.loads(km.read_text())
    known["odd"] = {"source": source, "installLocation": "/nowhere"}
    km.write_text(json.dumps(known))
    shown = {m["name"]: m for m in client.get("/api/library/plugins", headers=_H).json()["marketplaces"]}
    norm = shown["odd"]["normalizedSource"]
    r = client.post("/api/library/marketplaces/odd/confirm", json={"source": norm or "x", "confirm": True},
                    headers=_H)
    assert r.status_code in (400, 409)
    after = {m["name"]: m for m in client.get("/api/library/plugins", headers=_H).json()["marketplaces"]}
    assert after["odd"]["curation"] == "unconfirmed"


def test_confirm_resolves_https_hosts_and_refuses_internal(env, _dns) -> None:
    client, _, shared = env
    km = shared / "plugins" / "known_marketplaces.json"
    known = json.loads(km.read_text())
    known["internal"] = {"source": {"source": "git", "url": "https://git.corp.example/m.git"},
                         "installLocation": "/nowhere"}
    km.write_text(json.dumps(known))
    _dns["git.corp.example"] = ["100.101.102.103"]  # a tailnet address
    r = client.post("/api/library/marketplaces/internal/confirm",
                    json={"source": "https:git.corp.example/m", "confirm": True}, headers=_H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_MARKETPLACE_SOURCE_REFUSED"


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
    ("post", "/api/library/marketplaces/mine/confirm", {"source": "github:o/mine", "confirm": True}),
    ("post", "/api/library/plugins/reload", {}),
]


def _send(client: TestClient, method: str, url: str, body: dict | None):
    return client.request(method.upper(), url, headers=_H, **({"json": body} if body is not None else {}))


@pytest.mark.parametrize("method,url,body", MUTATIONS)
@pytest.mark.parametrize("via", ["assistant", "local", "local-trusted"])
def test_every_mutation_is_human_only(env, monkeypatch, method: str, url: str, body, via: str) -> None:
    """Fix round 1, finding 6: not even the operator's local-host opt-in admits these."""
    client, fleet, _ = env
    from server.api import library
    from server.auth import identity
    before = len(client.portal.call(_audit))
    if via == "local":
        monkeypatch.setattr(identity, "attested_front_end_hop", lambda _c: False)
    elif via == "local-trusted":
        monkeypatch.setenv("VIBERPIT_RULES_TRUST_LOCAL_HOST", "true")
        monkeypatch.setattr(identity, "attested_front_end_hop", lambda _c: False)
        assert identity.LOCAL_TRUSTED_VIA == "local-trusted"
        monkeypatch.setattr(library, "human_gate_via", lambda _r: identity.LOCAL_TRUSTED_VIA)
    else:
        monkeypatch.setattr(library, "human_gate_via", lambda _r: "assistant")
    r = _send(client, method, url, body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    if via == "local-trusted":
        assert "VIBERPIT_RULES_TRUST_LOCAL_HOST does not extend" in r.json()["detail"]["message"]
    assert fleet.calls == [] and len(client.portal.call(_audit)) == before


def test_require_human_strict_variant_is_one_parameter() -> None:
    from server.auth.identity import LOCAL_TRUSTED_VIA
    from server.sessions.session_actions import ActionError, require_human
    require_human("o@example.com", LOCAL_TRUSTED_VIA)  # default: the opt-in is honoured
    with pytest.raises(ActionError):
        require_human("o@example.com", LOCAL_TRUSTED_VIA, trust_local_host=False)
    require_human("o@example.com", None, trust_local_host=False)  # an attested person passes


@pytest.mark.parametrize("method,url,body,blocked", [
    (*MUTATIONS[0], True), (*MUTATIONS[1], True), (*MUTATIONS[2], False), (*MUTATIONS[3], False),
    (*MUTATIONS[4], True), (*MUTATIONS[5], True), (*MUTATIONS[6], False), (*MUTATIONS[7], True),
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


def test_reload_route_fans_out_and_audits(env) -> None:
    client, fleet, _ = env
    r = client.post("/api/library/plugins/reload", json={"holdOnCacheImpact": False}, headers=_H)
    assert r.status_code == 200
    assert fleet.calls == [("reload", False)]
    assert r.json() == {"op": "reload_plugins", "ok": True, "sessions": fleet.reload_reply}
    events = [e for e in client.portal.call(_audit) if e[0] == "library_plugins_reloaded"]
    assert events[0][1] == "owner@example.com"
    assert events[0][2] == {"holdOnCacheImpact": False, "sessions": fleet.reload_reply}
    client.post("/api/library/plugins/reload", json={}, headers=_H)
    assert fleet.calls[-1] == ("reload", True)  # default holds on a cache impact


def test_reload_route_ok_is_false_on_a_failed_row(env) -> None:
    client, fleet, _ = env
    fleet.reload_reply = [{"host": "dobby", "sessionId": "s1", "outcome": "applied"},
                          {"host": "rocky", "outcome": "failed", "failureCode": "host_unreachable"}]
    body = client.post("/api/library/plugins/reload", json={}, headers=_H).json()
    assert body["ok"] is False and body["sessions"] == fleet.reload_reply
    fleet.reload_reply = []
    assert client.post("/api/library/plugins/reload", json={}, headers=_H).json()["ok"] is False


def test_reload_is_not_behind_the_install_kill_switch(env, monkeypatch) -> None:
    client, fleet, _ = env
    monkeypatch.setenv("VIBERPIT_PLUGIN_INSTALL_ENABLED", "false")
    r = client.post("/api/library/plugins/reload", json={}, headers=_H)
    assert r.status_code == 200 and fleet.calls == [("reload", True)]


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
                        "components": None, "installed": False,
                        "source": "https://github.com/a/b"}
    assert good["source"] is None  # the UI shows the marketplace's own normalizedSource
    assert "unreviewed" not in json.dumps(body)


def test_browse_and_install_refresh_the_directory_cache_every_time(env, monkeypatch) -> None:
    """Fix round 1, finding 7: the (TTL-gated) refresh is asked before every read, not only
    when the cache file is missing."""
    client, _, _ = env
    asked: list[bool] = []

    def fake_refresh(**_k: Any) -> bool:
        asked.append(True)
        return False

    monkeypatch.setattr(plugin_cli, "refresh_available", fake_refresh)
    client.get("/api/library/plugins/browse", headers=_H)
    client.post(f"/api/library/plugins/reviewed@{DIR}/install", json={"confirm": True}, headers=_H)
    assert asked == [True, True]


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


def test_marketplace_add_validates_confirms_and_audits(env, shared) -> None:
    client, fleet, _ = env
    for bad in ("/tmp/local", "o/r%0A", "https://0x7f.1/a/b"):
        r = client.post("/api/library/marketplaces", json={"source": bad, "confirm": True}, headers=_H)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_MARKETPLACE_SOURCE_REFUSED"
    r = client.post("/api/library/marketplaces", json={"source": "o/newmkt"}, headers=_H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_CONFIRM_REQUIRED"
    assert fleet.calls == []
    fleet.reply = [{"host": "dobby", "outcome": "ok", "marketplace": "newmkt"},
                   {"host": "rocky", "outcome": "failed", "failureCode": "daemon_outdated"}]
    r = client.post("/api/library/marketplaces", json={"source": "O/NewMkt", "confirm": True}, headers=_H)
    assert r.status_code == 200 and r.json()["marketplaces"] == ["newmkt"]
    assert fleet.calls == [("marketplace_add", {"source": "O/NewMkt"})]
    _write_marketplace(shared, "newmkt", [], repo="O/NewMkt")  # what the CLI recorded
    # Confirmed at the source the owner typed: update is allowed and carries it.
    assert client.post("/api/library/marketplaces/newmkt/update", headers=_H).status_code == 200
    assert fleet.calls[-1] == ("marketplace_update", {"name": "newmkt", "expected_source": "github:o/newmkt"})
    assert client.delete("/api/library/marketplaces/newmkt", headers=_H).status_code == 200
    r = client.post("/api/library/marketplaces/newmkt/update", headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLUGIN_MARKETPLACE_NOT_CURATED"
    types = [e[0] for e in client.portal.call(_audit)]
    assert types.count("library_marketplace_added") == 1
    assert "library_marketplace_updated" in types and "library_marketplace_removed" in types
    added = next(e for e in client.portal.call(_audit) if e[0] == "library_marketplace_added")
    assert added[2]["source"] == "O/NewMkt" and added[2]["normalizedSource"] == "github:o/newmkt"
    assert added[2]["hosts"] == fleet.reply


def test_mixed_case_marketplace_is_confirmable_and_installable(env) -> None:
    """Fix round 1, finding 8: the CLI accepts case (Hello@MixedCase-Mkt), so do we."""
    client, fleet, shared = env
    _write_marketplace(shared, "MixedCase-Mkt", [{"name": "Hello", "source": "./h"}], repo="o/MixedCase-Mkt")
    assert _confirm(client, "MixedCase-Mkt").status_code == 200
    r = client.post("/api/library/plugins/Hello@MixedCase-Mkt/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    assert fleet.calls[-1] == ("install", {"plugin_id": "Hello@MixedCase-Mkt",
                                           "expected_source": "github:o/mixedcase-mkt"})


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
    _write_marketplace(shared, "claude-plugins-official", [], repo="anthropics/claude-plugins-official")
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "good@mine": [{"version": "1.0.0"}], "x@claude-plugins-official": [{"version": "9"}]}}))
    body = client.get("/api/library/plugins", headers=_H).json()
    rows = {p["id"]: p for p in body["plugins"]}
    assert (rows["good@mine"]["version"], rows["good@mine"]["autoUpdate"]) == ("1.0.0", False)
    assert rows["good@mine"]["source"] == {"source": "github", "repo": "o/mine"}
    assert rows["x@claude-plugins-official"]["autoUpdate"] is True
    # Configured but never confirmed: the official one too (fix round 1, finding 2).
    assert {m["name"]: m["curation"] for m in body["marketplaces"]} == {
        "mine": "confirmed", "claude-plugins-official": "unconfirmed"}
    assert body["installEnabled"] is True


def test_dead_marketplace_route_is_gone(env) -> None:
    client, _, _ = env
    assert client.get("/api/library/marketplace", headers=_H).status_code in (404, 405)


def test_settings_lists_the_kill_switch() -> None:
    from server.api.settings import _MANAGED
    assert ("plugin_install_enabled", "VIBERPIT_PLUGIN_INSTALL_ENABLED") in {r[:2] for r in _MANAGED}


def test_browse_entries_never_include_unreviewed(shared: Path) -> None:
    _write_directory(shared, {f"p{i}": _listing("none" if i % 2 else "done") for i in range(6)})
    out = scan.browse_entries({})
    assert [e["pluginId"] for e in out] == [f"p{i}@{DIR}" for i in (0, 2, 4)]


# =========================================================================== final-review fix wave

KELVIN = "K"  # KELVIN SIGN: str.lower() folds it to ASCII "k"
CYRILLIC_O = "о"  # looks like ASCII "o"


@pytest.mark.parametrize("source", [
    f"https://github.com/{KELVIN}evin/tools", f"{KELVIN}evin/tools",
    {"source": "github", "repo": f"{KELVIN}evin/tools"},
    {"source": "git", "url": f"https://github.com/{KELVIN}evin/tools.git"},
    f"https://gitlab.com/{KELVIN}evin/tools",
    f"https://github.com/kevin/t{CYRILLIC_O}{CYRILLIC_O}ls",
    {"source": "url", "url": f"https://git.ex{CYRILLIC_O}mple.com/m"},
])
def test_normalizer_refuses_non_ascii_before_case_folding(source: object) -> None:
    """Final review, item 2: lower() ran before any ASCII check, so a Kelvin-sign URL
    normalized to github:kevin/tools — the owner approved one string, the CLI fetched
    another. Non-ASCII never normalizes."""
    assert plugin_policy.normalize_source(source) is None


@pytest.mark.parametrize("source", [
    f"https://github.com/{KELVIN}evin/tools", f"{KELVIN}evin/tools",
    f"https://github.com/kevin/t{CYRILLIC_O}{CYRILLIC_O}ls", f"https://git.ex{CYRILLIC_O}mple.com/a/b",
])
def test_marketplace_source_refuses_non_ascii(source: str) -> None:
    with pytest.raises(PolicyRefusal) as ei:
        plugin_policy.check_marketplace_source(source)
    assert ei.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED
    assert "non-ASCII" in ei.value.message


@pytest.mark.parametrize("name,normalized,refused", [
    ("claude-plugins-official", "github:evil/x", True),
    ("Claude-Plugins-Official", "github:evil/x", True),
    ("anthropic-plugin-directory", "https:gitlab.com/evil/x", True),
    ("my-official-claude-tools", "github:evil/x", True),
    ("claude-plugins-official", "https:github.com/evil/x/sub", True),
    ("claude-plugins-official", "github:anthropics/claude-plugins-official", False),
    ("claude-plugins-official", "https:github.com/anthropics/x/sub", False),
    ("tools", "github:evil/x", False),
    ("claude-plugins-official", None, True),
])
def test_check_marketplace_name(name: str, normalized: str | None, refused: bool) -> None:
    if refused:
        with pytest.raises(PolicyRefusal) as ei:
            plugin_policy.check_marketplace_name(name, normalized)
        assert ei.value.code == plugin_policy.MARKETPLACE_NAME_RESERVED and ei.value.http == 400
    else:
        plugin_policy.check_marketplace_name(name, normalized)


def test_confirm_refuses_a_reserved_name_pointing_elsewhere(env) -> None:
    """Final review, item 5: a session points claude-plugins-official at evil/x; the owner
    is never asked to confirm an official-looking name for it. anthropics' own is fine."""
    client, _, shared = env
    _write_marketplace(shared, "claude-plugins-official", [], repo="evil/x")
    _write_marketplace(shared, "official-claude-stuff", [], repo="evil/stuff")
    for name in ("claude-plugins-official", "official-claude-stuff"):
        r = _confirm(client, name)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "PLUGIN_MARKETPLACE_NAME_RESERVED"
    shown = {m["name"]: m["curation"] for m in client.get("/api/library/plugins", headers=_H).json()["marketplaces"]}
    assert shown["claude-plugins-official"] == shown["official-claude-stuff"] == "unconfirmed"
    _write_marketplace(shared, "claude-plugins-official", [], repo="anthropics/claude-plugins-official")
    assert _confirm(client, "claude-plugins-official").status_code == 200


def test_marketplace_add_never_confirms_a_reserved_name_a_host_reports(env) -> None:
    """Item 5, add path: the name each host's CLI recorded is checked too."""
    client, fleet, _ = env
    fleet.reply = [{"host": "dobby", "outcome": "ok", "marketplace": "claude-plugins-official"},
                   {"host": "rocky", "outcome": "ok", "marketplace": "evilmkt"}]
    r = client.post("/api/library/marketplaces", json={"source": "evil/evilmkt", "confirm": True}, headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["marketplaces"] == ["evilmkt"] and body["ok"] is False
    dobby = body["hosts"][0]
    assert (dobby["outcome"], dobby["failureCode"]) == ("failed", "PLUGIN_MARKETPLACE_NAME_RESERVED")


def test_zero_hosts_is_never_success(env) -> None:
    """Final review, item 3: all() over no hosts was True."""
    client, fleet, _ = env
    fleet.reply = []
    r = client.post("/api/library/plugins/good@mine/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200 and r.json() == {"op": "install", "ok": False, "hosts": []}
    r = client.delete("/api/library/plugins/good@mine", headers=_H)
    assert r.json()["ok"] is False


def test_cli_env_routes_the_config_dir_through_helper_env(shared: Path, monkeypatch) -> None:
    """Final review, item 0: the config-dir pin comes from the canonical env builder
    (writes back to the real shared base, no account lookup, no token)."""
    from server.sessions import account_auth
    seen: list[tuple] = []
    real = account_auth.helper_env

    def spy(config_dir, account=None, *, writes_back=False):
        seen.append((config_dir, account, writes_back))
        return real(config_dir, account, writes_back=writes_back)

    monkeypatch.setattr(account_auth, "helper_env", spy)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    env = plugin_cli._env("n" * 32)
    assert env["CLAUDE_CONFIG_DIR"] == str(shared)
    assert seen == [(str(shared), "", True)]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# =========================================================================== #3312 follow-ups

@pytest.mark.parametrize("source", [
    {"source": "url", "url": "http://plugins.example.com/p.git"},
    {"source": "git", "url": "https://10.0.0.5/p.git"},
    {"source": "git", "url": "https://127.1/p.git"},
    {"source": "url", "url": "https://box.tail1234.ts.net/p.git"},
    {"source": "url", "url": "https://git.corp.local/p.git"},
    {"source": "url", "url": "https://localhost/p.git"},
    {"source": "url", "url": "https://intranet/p.git"},
    {"source": "url", "url": "https://user:pw@github.com/o/p.git"},
    {"source": "git", "url": "git@internal-git:o/p.git"},
    {"source": "git", "url": "internal-git:o/p.git"},
    {"source": "git", "url": "ssh://git@github.com/o/p.git"},
    "file:///etc/p",
    "http://plugins.example.com/p.git",
    {"source": "npm", "package": "p", "registry": "http://npm.internal:4873"},
    {"source": "url", "url": "https://github.com/o/p", "mirrors": ["https://192.168.1.4/p"]},
])
def test_plugin_entry_sources_follow_the_ssrf_rules(source: object) -> None:
    """#3312: a confirmed marketplace's catalog listing a plugin whose own source names an
    internal, http:// or tailnet address is refused on install, without DNS."""
    entry = {"name": "p", "source": source}
    assert plugin_policy.entry_source_refusal(entry)
    catalog = InstallCatalog(marketplaces={"mine": {"p": entry}}, sources={"mine": MINE})
    with pytest.raises(PolicyRefusal) as exc:
        plugin_policy.check_install("p@mine", CURATED, catalog)
    assert exc.value.code == plugin_policy.ENTRY_SOURCE_REFUSED


@pytest.mark.parametrize("source", [
    "./plugins/p", "plugins/p", {"source": "github", "repo": "o/p"},
    {"source": "url", "url": "https://github.com/o/p.git"},
    {"source": "git-subdir", "url": "https://gitlab.com/g/p", "path": "sub/dir"},
    {"source": "npm", "package": "@scope/p", "version": "^1.2.0"},
])
def test_plugin_entry_sources_that_fetch_from_public_https_pass(source: object) -> None:
    entry = {"name": "p", "source": source}
    assert plugin_policy.entry_source_refusal(entry) is None
    catalog = InstallCatalog(marketplaces={"mine": {"p": entry}}, sources={"mine": MINE})
    plugin_policy.check_install("p@mine", CURATED, catalog)


def test_entry_ssrf_check_never_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_policy, "_resolve", lambda host: pytest.fail("resolved"))
    assert plugin_policy.entry_source_refusal(
        {"source": {"source": "url", "url": "https://plugins.example.com/p.git"}}) is None


def test_tailnet_names_are_refused_as_marketplace_sources_too() -> None:
    with pytest.raises(PolicyRefusal) as exc:
        plugin_policy.check_marketplace_source("https://box.tail1234.ts.net/m.git")
    assert exc.value.code == plugin_policy.MARKETPLACE_SOURCE_REFUSED


def test_browse_hides_and_host_refuses_an_internal_entry_source(
        shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_marketplace(shared, "mine", [
        {"name": "good", "source": "./good"},
        {"name": "ssrf", "source": {"source": "url", "url": "http://169.254.169.254/p.git"}},
    ])
    assert [e["pluginId"] for e in scan.browse_entries(CURATED)] == ["good@mine"]
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", lambda *a, **k: pytest.fail("the CLI ran"))
    out = plugin_cli.run_op("install", {"plugin_id": "ssrf@mine", "expected_source": MINE})
    assert (out["outcome"], out["failureCode"]) == ("failed", plugin_policy.ENTRY_SOURCE_REFUSED)


@pytest.mark.parametrize("op,pid,runs", [
    ("install", f"x@{DIR}", 2), ("update", f"x@{DIR}", 2),
    ("install", "x@mine", 1), ("enable", f"x@{DIR}", 1), ("uninstall", f"x@{DIR}", 1),
])
def test_rpc_deadline_covers_every_cli_run_the_host_may_make(op: str, pid: str, runs: int) -> None:
    """#3312: a Directory install refreshes the cache (≤ CLI_TIMEOUT_S) and then installs
    (≤ CLI_TIMEOUT_S); one CLI run's deadline reported host_timeout while it still ran."""
    deadline = plugin_cli.rpc_timeout_s(op, {"plugin_id": pid})
    assert deadline > runs * plugin_cli.CLI_TIMEOUT_S
    assert deadline < (runs + 1) * plugin_cli.CLI_TIMEOUT_S
    assert plugin_cli.rpc_timeout_s("marketplace_update", {"name": "m"}) == plugin_cli.RPC_TIMEOUT_S


def test_remote_plugin_op_uses_the_per_op_deadline() -> None:
    from server.sessions.remote import RemoteOrchestrator
    seen: list[float] = []

    class _Client:
        async def call(self, method: str, **params: Any) -> Any:
            seen.append(params["timeout"])
            return {"outcome": "ok"}

    remote = RemoteOrchestrator.__new__(RemoteOrchestrator)
    remote._client = _Client()
    asyncio.run(remote.plugin_op("install", {"plugin_id": f"x@{DIR}"}))
    assert seen == [plugin_cli.rpc_timeout_s("install", {"plugin_id": f"x@{DIR}"})]


def test_failed_directory_refresh_is_retried_soon_not_after_the_ttl(
        shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#3312: the stamp was taken BEFORE the run, so a failed refresh was not retried for
    a whole TTL. Now success stamps the TTL, failure only the short retry window."""
    rcs = [1, 0, 0]
    calls: list[float] = []
    clock = {"now": 10_000.0}

    def fake_run(argv: list[str], **_k: Any) -> tuple[int, str]:
        calls.append(clock["now"])
        clock["now"] += 30  # the run itself takes time; stamps are taken when it ends
        return rcs.pop(0), ""

    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(plugin_cli, "_run", fake_run)
    monkeypatch.setattr(plugin_cli, "_last_available_refresh", 0.0)
    monkeypatch.setattr(plugin_cli, "_last_failed_refresh", 0.0)
    monkeypatch.setattr(plugin_cli.time, "monotonic", lambda: clock["now"])
    assert plugin_cli.refresh_available() is False  # ran and failed
    clock["now"] += plugin_cli.AVAILABLE_REFRESH_RETRY_S - 1
    assert plugin_cli.refresh_available() is False and len(calls) == 1  # inside the retry window
    clock["now"] += 2
    assert plugin_cli.refresh_available() is True and len(calls) == 2  # retried, long before the TTL
    clock["now"] += plugin_cli.AVAILABLE_REFRESH_TTL_S - 1
    assert plugin_cli.refresh_available() is False and len(calls) == 2  # TTL counts from the run's END
    clock["now"] += 2
    assert plugin_cli.refresh_available() is True and len(calls) == 3


def test_directory_browse_source_is_the_reviewed_repository(shared: Path) -> None:
    sha = "8ca22dba9a94f28898bbce59f2537ff4d87c747d"
    _write_directory(shared, {
        "pinned": {**_listing("done"), "source": {"type": "directory", "repository": {
            "url": "https://github.com/obra/superpowers", "reviewed_commit": sha}}},
        "bare": _listing("done"),
        "odd": {**_listing("done"), "source": {"type": "directory", "repository": {"url": 7}}},
    })
    by_id = {e["pluginId"]: e["source"] for e in scan.browse_entries({})}
    assert by_id == {f"pinned@{DIR}": f"https://github.com/obra/superpowers @ {sha[:12]}",
                     f"bare@{DIR}": "https://github.com/a/b", f"odd@{DIR}": None}


# ----------------------------------------------- Directory review verified live (#3314)

def test_directory_listing_passes_when_id_review_and_repo_match() -> None:
    plugin_policy.check_directory_listing(_listing("done"), _listing("done"))
    # A listing with no repository on BOTH sides passes on id + review alone.
    bare = {"id": "plugin_x", "checks": {"review": {"state": "done"}}, "source": {"type": "directory"}}
    plugin_policy.check_directory_listing(bare, dict(bare))


def test_directory_listing_refuses_a_repointed_source() -> None:
    live = _listing("done")
    cached = _listing("done", source={"type": "directory",
                                      "repository": {"url": "https://github.com/evil/b"}})
    with pytest.raises(PolicyRefusal) as exc:
        plugin_policy.check_directory_listing(cached, live)
    assert exc.value.code == "PLUGIN_UNREVIEWED"
    assert exc.value.reason == plugin_policy.REASON_DIRECTORY_MISMATCH
    # A repository on only one side is a mismatch too.
    bare = {"id": "plugin_x", "checks": {"review": {"state": "done"}}, "source": {"type": "directory"}}
    for c, lv in ((bare, _listing("done")), (_listing("done"), bare)):
        with pytest.raises(PolicyRefusal) as exc:
            plugin_policy.check_directory_listing(c, lv)
        assert exc.value.reason == plugin_policy.REASON_DIRECTORY_MISMATCH


@pytest.mark.parametrize("live", [_listing("none"), _listing("done", id="plugin_other"), "nope"])
def test_directory_listing_refuses_unreviewed_live_or_wrong_id_or_garbage(live: object) -> None:
    with pytest.raises(PolicyRefusal) as exc:
        plugin_policy.check_directory_listing(_listing("done"), live)
    assert exc.value.code == "PLUGIN_UNREVIEWED"
    assert exc.value.reason == plugin_policy.REASON_DIRECTORY_MISMATCH


def test_directory_install_verifies_the_listing_live(env, monkeypatch) -> None:
    client, fleet, _ = env
    asked: list[str] = []

    async def live(listing_id: str) -> dict | None:
        asked.append(listing_id)
        return _listing("done", id=listing_id)

    monkeypatch.setattr(directory_api, "fetch_listing", live)
    r = client.post(f"/api/library/plugins/reviewed@{DIR}/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    assert asked == ["id_reviewed"]


def test_directory_install_refuses_a_forged_cache(env, monkeypatch) -> None:
    client, fleet, _ = env

    async def live(listing_id: str) -> dict | None:
        return _listing("none", id=listing_id)

    monkeypatch.setattr(directory_api, "fetch_listing", live)
    r = client.post(f"/api/library/plugins/reviewed@{DIR}/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PLUGIN_UNREVIEWED"
    assert r.json()["detail"]["reason"] == "directory_mismatch"
    assert fleet.calls == []


def test_directory_install_fails_closed_when_the_api_is_down(env, monkeypatch) -> None:
    client, fleet, _ = env

    async def live(listing_id: str) -> dict | None:
        return None

    monkeypatch.setattr(directory_api, "fetch_listing", live)
    r = client.post(f"/api/library/plugins/reviewed@{DIR}/update", json={"confirm": True}, headers=_H)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PLUGIN_DIRECTORY_UNAVAILABLE"
    assert fleet.calls == []


def test_curated_marketplace_installs_never_call_the_directory(env, monkeypatch) -> None:
    client, fleet, _ = env

    async def live(listing_id: str) -> dict | None:
        raise AssertionError("the Directory API must not be called for a curated marketplace")

    monkeypatch.setattr(directory_api, "fetch_listing", live)
    r = client.post("/api/library/plugins/good@mine/install", json={"confirm": True}, headers=_H)
    assert r.status_code == 200
    assert len(fleet.calls) == 1


_EXPO_CACHED = {"id": "plugin_expo", "checks": {"review": {"state": "done"}},
                "source": {"type": "directory", "repository": {
                    "url": "https://github.com/expo/skills", "path": "plugins/expo",
                    "reviewed_commit": "170589a7"}}}


def _expo_live(**repo: Any) -> dict[str, Any]:
    return {"id": "plugin_expo", "checks": {"review": {"state": "done"}},
            "source": {"marketplace_slug": DIR, "type": "directory",
                       "repository": {"path": "plugins/expo", "reviewed_commit": "170589a7",
                                      "url": "https://github.com/expo/skills", **repo}}}


def test_directory_listing_passes_the_real_live_shape_with_marketplace_slug() -> None:
    plugin_policy.check_directory_listing(_EXPO_CACHED, _expo_live())


@pytest.mark.parametrize("repo", [{"path": "plugins/other"}, {"reviewed_commit": "deadbeef"},
                                  {"extra": "x"}])
def test_directory_listing_refuses_any_changed_repository_field(repo: dict) -> None:
    with pytest.raises(PolicyRefusal) as exc:
        plugin_policy.check_directory_listing(_EXPO_CACHED, _expo_live(**repo))
    assert exc.value.reason == plugin_policy.REASON_DIRECTORY_MISMATCH


def test_directory_listing_refuses_a_changed_source_type() -> None:
    live = _expo_live()
    live["source"]["type"] = "github"
    with pytest.raises(PolicyRefusal):
        plugin_policy.check_directory_listing(_EXPO_CACHED, live)


def _raw_cache(shared: Path, listings: list[dict], names: dict[str, str]) -> None:
    (shared / "plugins" / "plugin-directory-cache-v2.json").write_text(
        json.dumps({"schemaVersion": 2, "listings": listings, "entryNames": names}))


def test_duplicate_listing_ids_void_the_directory_cache(shared: Path) -> None:
    _raw_cache(shared, [{**_listing("done"), "id": "dup"}, {**_listing("done"), "id": "dup"}],
               {"dup": "x"})
    assert scan.directory_listings() is None


def test_two_ids_mapped_to_one_entry_name_void_the_directory_cache(shared: Path) -> None:
    _raw_cache(shared, [{**_listing("done"), "id": "a"}, {**_listing("done"), "id": "b"}],
               {"a": "x", "b": "x"})
    assert scan.directory_listings() is None


def test_install_from_a_duplicate_cache_is_refused_and_never_fanned_out(env) -> None:
    client, fleet, shared = env
    _raw_cache(shared, [{**_listing("done"), "id": "a"}, {**_listing("done"), "id": "b"}],
               {"a": "reviewed", "b": "reviewed"})
    r = client.post(f"/api/library/plugins/reviewed@{DIR}/install", json={"confirm": True}, headers=_H)
    assert r.status_code in (404, 409)
    assert r.json()["detail"]["code"] in ("PLUGIN_NOT_FOUND", "PLUGIN_UNREVIEWED")
    assert fleet.calls == []
