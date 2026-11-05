"""D-16c contract test: the ``reload_plugins`` control request against the INSTALLED binary.

``ClaudeAgentDriver.reload_plugins`` sends ``plugin_reload.request(...)`` raw on the control
channel and reduces the reply with ``plugin_reload.outcome``. This pins, against the binary
(constitution III): the reply carries ``commands``/``plugins``/``error_count`` (and ``held``
when ``hold_on_cache_impact`` was sent); a session with no output tokens is never held; a
plugin installed beforehand shows up (``plugins[].name`` and a ``hello:``-prefixed command);
the binary accepts a non-boolean flag (no control ``error``), so the driver must send a strict bool.

It can never spend tokens: the ``test_settings_cli_contract._Cli`` harness writes control
requests only, runs on a throwaway HOME with no credential and a closed loopback
``ANTHROPIC_BASE_URL``, and asserts no ``assistant`` frame ever appears. Skips without a
binary (``VIBERPIT_CONTRACT_CLAUDE_BIN`` or the newest under ``~/.local/share/claude``).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from server.sessions import plugin_reload
from server.tests.test_plugin_cli_contract import BINARY
from server.tests.test_rewind_cli_contract import _cli_path_env
from server.tests.test_settings_cli_contract import _Cli

pytestmark = pytest.mark.slow

_MKT_NAME = "d16c-reload-contract"


def _write_marketplace(root: Path) -> Path:
    mkt = root / "mkt"
    hello = mkt / "plugins" / "hello"
    (mkt / ".claude-plugin").mkdir(parents=True)
    (hello / ".claude-plugin").mkdir(parents=True)
    (hello / "commands").mkdir()
    (hello / ".claude-plugin" / "plugin.json").write_text('{"name": "hello", "version": "1.0.0"}')
    (hello / "commands" / "hi.md").write_text("---\ndescription: hi\n---\nsay hi\n")
    (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": _MKT_NAME, "owner": {"name": "test"},
        "plugins": [{"name": "hello", "description": "hi", "version": "1.0.0",
                     "source": "./plugins/hello"}]}))
    return mkt


def _plugin_cli(binary: str, env: dict[str, str], *words: str) -> None:
    done = subprocess.run([binary, "plugin", *words, "--scope", "user", "--json"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    first = done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""
    assert json.loads(first)["outcome"] == "ok", (words, done.stdout, done.stderr)


def test_reload_plugins_reply_shape_and_hold_passthrough(tmp_path: Path) -> None:
    if BINARY is None:
        pytest.skip("no claude binary (set VIBERPIT_CONTRACT_CLAUDE_BIN)")
    cfg, home = tmp_path / "cfg", tmp_path / "home"
    cfg.mkdir()
    home.mkdir()
    mkt = _write_marketplace(tmp_path)
    env = {
        "PATH": _cli_path_env(BINARY), "HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1",
    }
    assert not os.path.exists(cfg / "plugins")
    _plugin_cli(BINARY, env, "marketplace", "add", str(mkt))
    _plugin_cli(BINARY, env, "install", f"hello@{_MKT_NAME}")

    cli = _Cli(BINARY, cfg=str(cfg))
    try:
        cli.ok({"subtype": "initialize"})
        reply = cli.ok(plugin_reload.request(True))
        for key in ("commands", "plugins", "error_count"):
            assert key in reply, reply
        assert reply.get("held") is False, reply  # no output tokens: never held
        reduced = plugin_reload.outcome(reply)
        assert reduced["outcome"] == "applied", reduced
        assert "hello" in reduced["plugins"], reply
        assert any(str(c.get("name", "")).startswith("hello:")
                   for c in reply["commands"] if isinstance(c, dict)), reply["commands"]

        again = cli.ok(plugin_reload.request(False))
        assert plugin_reload.outcome(again)["outcome"] == "applied", again

        # Observed on 2.1.318: the binary does NOT reject a non-boolean flag (the driver
        # therefore sends a strict bool). Any truthy value answers ``held``; omitted/null does not.
        sloppy = cli.ok({"subtype": "reload_plugins", "hold_on_cache_impact": "yes"})
        assert sloppy.get("held") is False, sloppy
        bare = cli.ok({"subtype": "reload_plugins"})
        assert "held" not in bare and "plugins" in bare, bare
    finally:
        cli.close()
