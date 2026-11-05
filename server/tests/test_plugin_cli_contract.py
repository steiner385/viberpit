"""No-model contract test: ``plugin_cli`` against the REAL ``claude`` binary (D-16c).

Pins the wrapper's assumptions about ``claude plugin … --json`` (docs/superpowers/specs/
2026-11-04-d16-verification.md §1): the result-line parse for add, install, disable, the
no-op disable, update, uninstall and remove; the ``failureCode`` → outcome mapping; the
command-source refusal; and that a symlinked account ``settings.json``/``plugins`` survives.

Everything runs in a throwaway HOME and ``CLAUDE_CONFIG_DIR``; ``~/.claude`` and every real
account dir are never touched. The local ``directory`` marketplace is a fixture of THIS
test only — the route refuses local paths — so it is added with a hand-built argv
through the wrapper's own subprocess seam and parser. Skips without the binary.
"""
from __future__ import annotations

import json
import os
import pwd
import re
from pathlib import Path

import pytest

from server.library import plugin_cli

pytestmark = pytest.mark.slow

_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_VERSIONS = _REAL_HOME / ".local" / "share" / "claude" / "versions"


def _binary() -> str | None:
    pinned = os.environ.get("VIBERPIT_CONTRACT_CLAUDE_BIN")
    if pinned:
        return pinned if os.access(pinned, os.X_OK) else None
    try:
        names = [n for n in os.listdir(_VERSIONS) if re.fullmatch(r"\d+\.\d+\.\d+", n)]
    except OSError:
        return None
    if not names:
        return None
    newest = max(names, key=lambda n: tuple(int(p) for p in n.split(".")))
    path = _VERSIONS / newest
    return str(path) if os.access(path, os.X_OK) else None


BINARY = _binary()


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if BINARY is None:
        pytest.skip("no claude binary under ~/.local/share/claude/versions")
    home = tmp_path / "home"
    shared = home / ".claude"
    (shared / "plugins").mkdir(parents=True)
    (shared / "settings.json").write_text("{}")
    # An account dir linking the shared assets the way configdir.reconcile_links does.
    acct = tmp_path / "acct"
    acct.mkdir()
    (acct / "settings.json").symlink_to(shared / "settings.json")
    (acct / "plugins").symlink_to(shared / "plugins")
    mkt = tmp_path / "mkt"
    (mkt / ".claude-plugin").mkdir(parents=True)
    hello = mkt / "plugins" / "hello"
    (hello / ".claude-plugin").mkdir(parents=True)
    (hello / "commands").mkdir()
    (hello / ".claude-plugin" / "plugin.json").write_text('{"name": "hello", "version": "1.0.0"}')
    (hello / "commands" / "hi.md").write_text("---\ndescription: hi\n---\nsay hi\n")
    (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "d16c-contract", "owner": {"name": "test"},
        "plugins": [
            {"name": "hello", "description": "hi", "version": "1.0.0", "source": "./plugins/hello"},
            {"name": "cmdp", "description": "c", "version": "1.0.0",
             "source": {"source": "command", "command": "echo should-never-run"}},
        ]}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.setenv("VIBERPIT_HELPER_CWD", str(tmp_path / "cwd"))
    monkeypatch.setattr(plugin_cli, "_claude_bin", lambda: BINARY)
    assert plugin_cli.paths.shared_base() == shared
    return shared, acct, mkt


def _add_local(mkt: Path) -> dict:
    """``marketplace add <dir>`` through the wrapper's seam + parser (the route would refuse
    a local path; only this test may add one)."""
    assert BINARY is not None
    nonce = "contract-" + os.urandom(4).hex()
    argv = [BINARY, "plugin", "marketplace", "add", str(mkt), "--scope", "user", "--json"]
    plugin_cli.assert_safe_argv(argv)
    rc, stdout = plugin_cli._run(argv, env=plugin_cli._env(nonce), cwd=plugin_cli._cwd(),
                                 timeout=plugin_cli.CLI_TIMEOUT_S)
    return plugin_cli.parse_result("marketplace_add", rc, stdout, nonce)


def test_cli_contract_round_trip(sandbox) -> None:
    shared, acct, mkt = sandbox
    pid = "hello@d16c-contract"

    added = _add_local(mkt)
    assert (added["outcome"], added.get("marketplace")) == ("ok", "d16c-contract"), added
    assert _add_local(mkt)["outcome"] in ("ok", "noop")  # re-adding is idempotent
    out =plugin_cli.run_op("install", {"plugin_id": pid})
    assert out["outcome"] == "ok" and out["version"] == "1.0.0", out
    registry = json.loads((shared / "plugins" / "installed_plugins.json").read_text())
    install_path = registry["plugins"][pid][0]["installPath"]
    assert install_path.startswith(str(shared / "plugins" / "cache")), install_path

    assert plugin_cli.run_op("disable", {"plugin_id": pid})["outcome"] == "ok"
    noop = plugin_cli.run_op("disable", {"plugin_id": pid})
    assert (noop["outcome"], noop["failureCode"]) == ("noop", "already_in_goal_state")
    assert json.loads((shared / "settings.json").read_text())["enabledPlugins"][pid] is False
    assert plugin_cli.run_op("enable", {"plugin_id": pid})["outcome"] == "ok"

    upd = plugin_cli.run_op("update", {"plugin_id": pid})
    assert upd["outcome"] == "ok" and upd["version"] == "1.0.0", upd

    missing = plugin_cli.run_op("install", {"plugin_id": "nosuch@d16c-contract"})
    assert (missing["outcome"], missing["failureCode"]) == ("failed", "not_found")

    refused = plugin_cli.run_op("install", {"plugin_id": "cmdp@d16c-contract"})
    assert (refused["outcome"], refused["failureCode"]) == ("failed", "PLUGIN_COMMAND_SOURCE_REFUSED")
    assert "cmdp@d16c-contract" not in json.loads(
        (shared / "plugins" / "installed_plugins.json").read_text())["plugins"]

    assert plugin_cli.run_op("uninstall", {"plugin_id": pid})["outcome"] == "ok"
    again = plugin_cli.run_op("uninstall", {"plugin_id": pid})
    assert (again["outcome"], again["failureCode"]) == ("noop", "not_installed")

    assert plugin_cli.run_op("marketplace_update", {"name": "d16c-contract"})["outcome"] == "ok"
    assert plugin_cli.run_op("marketplace_remove", {"name": "d16c-contract"})["outcome"] == "ok"
    gone = plugin_cli.run_op("marketplace_remove", {"name": "d16c-contract"})
    assert (gone["outcome"], gone["failureCode"]) == ("noop", "not_configured")

    # The CLI writes THROUGH an account's links; it never replaces them with files.
    assert (acct / "settings.json").is_symlink() and (acct / "plugins").is_symlink()
    assert (shared / "settings.json").is_file() and not (shared / "settings.json").is_symlink()
