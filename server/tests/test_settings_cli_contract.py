"""D-14 contract test against the INSTALLED ``claude`` binary (constitution I/III).

The Claude driver writes the CLI's flag-layer settings live with the raw
``apply_flag_settings`` control request (``ClaudeAgentDriver.apply_flag_settings``) and
reads which models have fast mode from ``initialize.models[].supportsFastMode``
(``server/sessions/flag_settings.py``). This pins, against the binary:

- ``initialize`` lists models with ``supportsFastMode`` and ``supportedEffortLevels``;
- ``{"fastMode": true|false}`` and ``{"effortLevel": <level>|null}`` are accepted and
  read back by ``get_settings``, each reply awaited before the next write (the spike saw
  stale reads from back-to-back frames);
- a malformed request is a control ``error`` (the driver's ``SETTINGS_REFUSED``).

It can never spend tokens (the D-9 harness, ``test_rewind_cli_contract``):
- no user message is ever written to the process, only control requests;
- the process runs with a fresh ``CLAUDE_CONFIG_DIR`` and ``HOME`` and an env built from
  scratch, so it holds no credential at all;
- ``ANTHROPIC_BASE_URL`` points at a closed loopback port as a second fence.

Skipped when no ``claude`` resolves (``VIBERPIT_CLAUDE_BIN``, PATH, then the native
installer's well-known paths under ``HOME``). Under an isolated ``HOME`` set
``VIBERPIT_CLAUDE_BIN`` to run it.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator

import pytest

from server.sessions import flag_settings as fs
from server.sessions.claude_bin import resolve_claude_bin
from server.tests.test_rewind_cli_contract import _cli_path_env

pytestmark = pytest.mark.slow

_TIMEOUT_S = 60.0


class _Cli:
    """One throwaway stream-json CLI. ``ask`` writes one control request and waits for
    ITS reply before returning, so no two writes are ever in flight together."""

    def __init__(self, binary: str, extra_args: list[str] | None = None) -> None:
        self._root = tempfile.mkdtemp(prefix="d14-settings-contract.")
        cwd, cfg, home = (os.path.join(self._root, n) for n in ("cwd", "cfg", "home"))
        for p in (cwd, cfg, home):
            os.mkdir(p)
        env = {
            "PATH": _cli_path_env(binary),
            "HOME": home,
            "CLAUDE_CONFIG_DIR": cfg,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        }
        argv = [binary, "-p", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose", "--session-id", str(uuid.uuid4()),
                *(extra_args or [])]
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, cwd=cwd, env=env)
        self._buf = b""
        self._n = 0

    def _lines(self, deadline: float) -> Iterator[dict]:
        assert self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                assert msg.get("type") != "assistant", "the contract test must never run a turn"
                yield msg
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                return
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            self._buf += chunk

    def ask(self, request: dict) -> dict:
        self._n += 1
        rid = f"r{self._n}"
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps({"type": "control_request", "request_id": rid,
                                            "request": request}) + "\n").encode())
        self._proc.stdin.flush()
        for msg in self._lines(time.monotonic() + _TIMEOUT_S):
            if msg.get("type") == "control_response" and msg["response"].get("request_id") == rid:
                return msg["response"]
        stderr = self._proc.stderr.read(4000) if self._proc.stderr else b""
        raise AssertionError(f"no reply to {request.get('subtype')}; stderr: {stderr!r}")

    def ok(self, request: dict) -> dict:
        resp = self.ask(request)
        assert resp["subtype"] == "success", resp
        return resp.get("response") or {}

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        finally:
            for f in (self._proc.stdout, self._proc.stderr):
                if f is not None:
                    f.close()
            shutil.rmtree(self._root, ignore_errors=True)


@pytest.fixture(scope="module")
def binary() -> str:
    path = resolve_claude_bin()
    if path is None:
        pytest.skip("no installed claude binary (set VIBERPIT_CLAUDE_BIN)")
    return path


@pytest.fixture
def cli(binary: str) -> Iterator[_Cli]:
    c = _Cli(binary)
    try:
        yield c
    finally:
        c.close()


def _apply(cli: _Cli, patch: dict) -> None:
    # The driver's exact request shape (ClaudeAgentDriver.apply_flag_settings).
    cli.ok({"subtype": "apply_flag_settings", "settings": patch})


def test_initialize_reports_fast_mode_per_model(cli: _Cli) -> None:
    init = cli.ok({"subtype": "initialize", "hooks": None})
    fast = fs.fast_mode_models(init)
    assert fast, init.get("models")
    # The default row is matched by our "no model" lookup, and its effort list is the
    # one the driver validates a live effort against.
    assert fs.model_entry(init, None) is not None
    assert "high" in (fs.effort_levels(init, None) or [])
    assert init.get("fast_mode_state") in ("on", "off", "cooldown")


def test_fast_mode_patches_are_accepted_and_read_back(cli: _Cli) -> None:
    cli.ok({"subtype": "initialize", "hooks": None})
    _apply(cli, {fs.FAST_MODE_KEY: True})
    assert cli.ok({"subtype": "get_settings"})["effective"][fs.FAST_MODE_KEY] is True
    _apply(cli, {fs.FAST_MODE_KEY: False})
    assert cli.ok({"subtype": "get_settings"})["effective"][fs.FAST_MODE_KEY] is False


def test_effort_patches_are_applied_live_and_cleared_by_null(cli: _Cli) -> None:
    cli.ok({"subtype": "initialize", "hooks": None})
    before = cli.ok({"subtype": "get_settings"})["applied"]["effort"]
    target = "high" if before != "high" else "low"
    _apply(cli, {fs.EFFORT_KEY: target})
    assert cli.ok({"subtype": "get_settings"})["applied"]["effort"] == target
    _apply(cli, {fs.EFFORT_KEY: None})
    after = cli.ok({"subtype": "get_settings"})
    assert after["applied"]["effort"] == before
    assert after["effective"].get(fs.EFFORT_KEY) is None


def test_a_malformed_request_is_a_control_error(cli: _Cli) -> None:
    cli.ok({"subtype": "initialize", "hooks": None})
    resp = cli.ask({"subtype": "apply_flag_settings"})
    assert resp["subtype"] == "error" and "settings" in resp["error"], resp
