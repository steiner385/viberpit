"""D-13 contract test against the INSTALLED ``claude`` binary (constitution I/III).

ViberPit shares the operator's user memory by symlinking ``$CLAUDE_CONFIG_DIR/CLAUDE.md``
to ``shared_base()/CLAUDE.md`` (``viberpit.configdir.SHARE_ALLOWLIST``). That only works
while the CLI resolves the user file from ``CLAUDE_CONFIG_DIR`` (not HOME), follows a
symlinked user file, and treats a DANGLING link as absent — ``reconcile_links`` creates
the link before the shared file exists. This pins all three, plus that the stateless
helpers' ``setting_sources=[]`` still loads no user memory through the link
(D-13 spike ``docs/superpowers/specs/2026-11-04-d13-memory-verification.md`` §1, §4).

It can never spend tokens (the D-9 harness, ``test_rewind_cli_contract``):
- no user message is ever written to the process, only control requests;
- the process runs with a fresh ``CLAUDE_CONFIG_DIR`` and ``HOME`` and an env built from
  scratch, so it holds no credential at all;
- ``ANTHROPIC_BASE_URL`` points at a closed loopback port as a second fence, and
  ``get_context_usage`` asks for ``detail: "summary"``, which answers locally.

Skipped when no ``claude`` resolves (``VIBERPIT_CLAUDE_BIN``, PATH, then the native
installer's well-known paths under ``HOME``). Under an isolated ``HOME`` set
``VIBERPIT_CLAUDE_BIN`` to run it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid

import pytest

from server.sessions.claude_bin import resolve_claude_bin
from server.sessions.helper_call import stateless_helper_options
from server.tests.test_rewind_cli_contract import _cli_path_env

pytestmark = pytest.mark.slow

_TIMEOUT_S = 60
_USER_TEXT = "# shared user memory (D-13 contract)\nAlways answer in haiku.\n"


def _memory_files(binary: str, *, shared_exists: bool,
                  setting_sources: list[str] | None = None) -> tuple[list[dict], str]:
    """Spawn a throwaway stream-json CLI whose ``$CLAUDE_CONFIG_DIR/CLAUDE.md`` is a
    symlink to a temp "shared" file (missing when ``shared_exists`` is False), with a
    cwd OUTSIDE ``HOME`` so the project walk cannot reach ``$HOME/.claude/CLAUDE.md``.
    Return ``get_context_usage.memoryFiles`` and the link path. Temp dirs are removed."""
    path_env = _cli_path_env(binary)
    root = tempfile.mkdtemp(prefix="d13-memory-contract.")
    try:
        cwd, cfg, home, shared = (os.path.join(root, n)
                                  for n in ("cwd", "cfg", "home", "shared"))
        for p in (cwd, cfg, home, shared):
            os.mkdir(p)
        target = os.path.join(shared, "CLAUDE.md")
        if shared_exists:
            with open(target, "w") as fh:
                fh.write(_USER_TEXT)
        link = os.path.join(cfg, "CLAUDE.md")
        os.symlink(target, link)
        env = {
            "PATH": path_env,
            "HOME": home,
            "CLAUDE_CONFIG_DIR": cfg,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        }
        argv = [binary, "-p", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose", "--session-id", str(uuid.uuid4())]
        if setting_sources is not None:  # one argument, exactly as the SDK's subprocess_cli.py
            argv.append(f"--setting-sources={','.join(setting_sources)}")
        frames = [
            {"type": "control_request", "request_id": "init",
             "request": {"subtype": "initialize", "hooks": None}},
            {"type": "control_request", "request_id": "ctx",
             "request": {"subtype": "get_context_usage", "detail": "summary"}},
        ]
        proc = subprocess.run(
            argv, input="".join(json.dumps(f) + "\n" for f in frames), capture_output=True,
            text=True, cwd=cwd, env=env, timeout=_TIMEOUT_S, check=False)
        replies: dict[str, dict] = {}
        for line in proc.stdout.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            assert msg.get("type") != "assistant", "the contract test must never run a turn"
            if msg.get("type") == "control_response":
                resp = msg["response"]
                replies[resp["request_id"]] = resp
        ctx = replies.get("ctx")
        assert ctx is not None, f"no get_context_usage reply; stderr: {proc.stderr[-2000:]}"
        assert ctx["subtype"] == "success", ctx
        return ctx["response"]["memoryFiles"], link
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def binary() -> str:
    path = resolve_claude_bin()
    if path is None:
        pytest.skip("no installed claude binary (set VIBERPIT_CLAUDE_BIN)")
    return path


def test_a_linked_user_file_loads_as_user_memory_outside_home(binary):
    rows, link = _memory_files(binary, shared_exists=True)
    user = [r for r in rows if r["type"] == "User"]
    # The row is the LINK path, not the resolved target (spike §3, probes F/G).
    assert [r["path"] for r in user] == [link], rows
    assert user[0]["tokens"] > 0


def test_a_dangling_user_link_loads_nothing_and_does_not_fail(binary):
    rows, link = _memory_files(binary, shared_exists=False)
    assert [r for r in rows if r["type"] == "User"] == [], rows
    assert all(r["path"] != link for r in rows), rows


def test_stateless_helpers_still_load_no_user_memory_through_the_link(binary):
    sources = stateless_helper_options()["setting_sources"]
    assert sources == []
    rows, _ = _memory_files(binary, shared_exists=True, setting_sources=sources)
    assert rows == []
