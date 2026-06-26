"""Regression tests for the daemon import-time self-deadlock.

The session daemon's `/info` and `/healthz` handlers only need `running_git_sha()`,
a trivial helper. Historically it lived in `server.app`, whose module-level
`app = create_app()` runs a *synchronous, blocking* `_daemon_compatible()` probe
against the daemon's own socket. When the daemon (single-threaded asyncio) imported
`server.app` to grab that helper, the probe blocked the only event-loop thread — so
the daemon could not answer the very probe it had issued, and every other RPC
(including `list_sessions`, which backs the dashboard's session list) hung forever.

Two invariants prevent the whole class of bug:
  1. The helper lives in a side-effect-free module (`server.version`); importing it
     never builds the edge app.
  2. `_daemon_compatible()` refuses to do blocking sync I/O when a live event loop is
     running on the thread (defense in depth).
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_git_sha_helper_import_does_not_build_edge_app():
    """Importing the git-sha helper must NOT drag in `server.app` / create_app().

    Run in a fresh interpreter so an already-imported `server.app` (pulled in by
    other tests in this process) can't mask the regression.
    """
    code = (
        "import sys\n"
        "import server.version\n"
        "server.version.running_git_sha()\n"
        "leaked = sorted(m for m in sys.modules if m == 'server.app')\n"
        "assert not leaked, f'importing the helper built the edge app: {leaked}'\n"
        "print('OK')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(_REPO_ROOT),
    )
    assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert "OK" in out.stdout


def test_daemon_imports_git_sha_from_side_effect_free_module():
    """The daemon must source the helper from `server.version`, not `server.app`."""
    daemon_src = (_REPO_ROOT / "server" / "ipc" / "daemon.py").read_text()
    assert "from server.app import running_git_sha" not in daemon_src, (
        "daemon.py still imports running_git_sha from server.app — re-introduces "
        "the import-time self-deadlock"
    )
    assert "from server.version import running_git_sha" in daemon_src


@pytest.mark.asyncio
async def test_daemon_compatible_does_not_block_a_running_loop(monkeypatch):
    """Under a live event loop, the sync probe must short-circuit, not block it.

    Socket is reported present so the loop guard is the only thing that can stop a
    blocking probe; `time.sleep` and `httpx.Client` are booby-trapped to prove no
    blocking I/O ran.
    """
    import time

    import httpx

    # Neutralize the import-time daemon auto-detect so importing `server.app` on a
    # host with a live (or wedged) daemon socket can't raise during collection.
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/tmp/ccswitch-test-no-daemon.sock")
    from server import app as app_module

    monkeypatch.setattr(app_module, "_daemon_present", lambda _p: True)

    def _boom_sleep(*_a, **_k):
        raise AssertionError("time.sleep ran — the blocking retry loop executed under a live loop")

    def _boom_client(*_a, **_k):
        raise AssertionError("httpx.Client constructed — sync I/O ran under a live loop")

    monkeypatch.setattr(time, "sleep", _boom_sleep)
    monkeypatch.setattr(httpx, "Client", _boom_client)

    # We are inside a running loop (async test) — must return False without blocking.
    assert app_module._daemon_compatible("/tmp/does-not-matter.sock") is False
