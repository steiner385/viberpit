"""D-9 contract test against the INSTALLED ``claude`` binary (constitution I/III).

``rewind_files`` (with ``dry_run``) and ``rewind_conversation`` are internal
control requests: neither SDK exposes them with the shape the driver needs. This
test sends the adapter's exact requests (``server.sessions.rewind`` builders) to a
real stream-json process and checks the replies still map to the documented codes.

It can never spend tokens:
- no user message is ever written to the process, only control requests;
- the process runs with a fresh, empty ``CLAUDE_CONFIG_DIR`` and ``HOME`` and an
  env built from scratch, so it holds no credential at all;
- ``ANTHROPIC_BASE_URL`` points at a closed loopback port as a second fence.

It needs only the binary, no auth. Skipped when no ``claude`` resolves
(``server.sessions.claude_bin.resolve_claude_bin``: ``VIBERPIT_CLAUDE_BIN``, PATH,
then the native installer's well-known paths under ``HOME``). Under an isolated
``HOME`` set ``VIBERPIT_CLAUDE_BIN`` to run it.
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
from server.sessions.rewind import (REWIND_NO_CHECKPOINT, REWIND_TARGET_NOT_FOUND,
                                    conversation_request, files_request, map_cli_error)

pytestmark = pytest.mark.slow

BOGUS = "00000000-0000-4000-8000-000000000000"
_TIMEOUT_S = 60


def _cli_path_env(binary: str) -> str:
    """The minimal PATH for the CLI. A native binary needs nothing extra. A script
    wrapper (``#!/usr/bin/env node``, an npm install) gets its interpreter's dir on
    PATH; when that interpreter can't be found the test SKIPS — a wrapper must never
    turn into a contract failure."""
    path = "/usr/bin:/bin"
    with open(binary, "rb") as fh:
        head = fh.readline(512)
    if not head.startswith(b"#!"):
        return path
    parts = head[2:].decode(errors="replace").split()
    if not parts:
        pytest.skip(f"{binary} is a script with an empty shebang")
    if os.path.basename(parts[0]) == "env":
        args = [a for a in parts[1:] if not a.startswith("-")]
        if not args:
            pytest.skip(f"{binary} is a script with an unparseable shebang")
        interp = args[0]
    else:
        interp = parts[0]
    resolved = interp if os.path.isabs(interp) else shutil.which(interp)
    if resolved is None or not os.access(resolved, os.X_OK):
        pytest.skip(f"{binary} is a script needing {interp!r}, which was not found on PATH")
    return f"{os.path.dirname(resolved)}:{path}"


def _run_cli(binary: str, requests: list[dict]) -> dict[str, dict]:
    """Start a throwaway stream-json session, send ``requests`` as control requests
    (after the SDK's ``initialize``), close stdin, and return each ``control_response``
    by request id. Every temp dir is removed afterwards."""
    path_env = _cli_path_env(binary)
    root = tempfile.mkdtemp(prefix="d9-rewind-contract.")
    try:
        cwd, cfg, home = (os.path.join(root, n) for n in ("cwd", "cfg", "home"))
        for p in (cwd, cfg, home):
            os.mkdir(p)
        env = {
            "PATH": path_env,
            "HOME": home,
            "CLAUDE_CONFIG_DIR": cfg,
            "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        }
        frames = [{"type": "control_request", "request_id": "init",
                   "request": {"subtype": "initialize", "hooks": None}}]
        frames += [{"type": "control_request", "request_id": f"r{i}", "request": r}
                   for i, r in enumerate(requests)]
        stdin = "".join(json.dumps(f) + "\n" for f in frames)
        proc = subprocess.run(
            [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--session-id", str(uuid.uuid4()), "--setting-sources", ""],
            input=stdin, capture_output=True, text=True, cwd=cwd, env=env,
            timeout=_TIMEOUT_S, check=False)
        out: dict[str, dict] = {}
        for line in proc.stdout.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            assert msg.get("type") != "assistant", "the contract test must never run a turn"
            if msg.get("type") == "control_response":
                resp = msg["response"]
                out[resp["request_id"]] = resp
        assert "init" in out, f"no initialize reply; stderr: {proc.stderr[-2000:]}"
        return out
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def binary() -> str:
    path = resolve_claude_bin()
    if path is None:
        pytest.skip("no installed claude binary (set VIBERPIT_CLAUDE_BIN)")
    return path


def test_bogus_ids_get_the_documented_replies(binary):
    replies = _run_cli(binary, [
        conversation_request(BOGUS, last_seen=None),
        files_request(BOGUS, dry_run=True),
        files_request(BOGUS, dry_run=False),
    ])

    conv = replies["r0"]
    assert conv["subtype"] == "success"
    assert conv["response"]["rewound"] is False
    assert conv["response"]["reason"] == "target_not_found"
    assert map_cli_error(conv["response"]).code == REWIND_TARGET_NOT_FOUND

    dry = replies["r1"]
    assert dry["subtype"] == "success"
    assert dry["response"]["canRewind"] is False
    assert "No file checkpoint found" in dry["response"]["error"]
    assert map_cli_error(dry["response"]).code == REWIND_NO_CHECKPOINT

    # Without dry_run the same refusal is a control ERROR (the SDK raises its text).
    real = replies["r2"]
    assert real["subtype"] == "error"
    assert "No file checkpoint found" in real["error"]
    assert map_cli_error({"error": real["error"]}).code == REWIND_NO_CHECKPOINT


def test_wrapper_path_native_binary_needs_nothing(tmp_path):
    exe = tmp_path / "claude"
    exe.write_bytes(b"\x7fELF\x02\x01")
    assert _cli_path_env(str(exe)) == "/usr/bin:/bin"


def test_wrapper_path_carries_the_interpreter_dir(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/usr/bin/env sh\nexit 0\n")
    sh = shutil.which("sh")
    assert sh is not None
    assert _cli_path_env(str(exe)) == f"{os.path.dirname(sh)}:/usr/bin:/bin"


def test_wrapper_without_its_interpreter_skips(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/usr/bin/env d9-no-such-interpreter\n")
    with pytest.raises(pytest.skip.Exception, match="d9-no-such-interpreter"):
        _cli_path_env(str(exe))
