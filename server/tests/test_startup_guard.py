"""#3386: the edge and daemon are non-dumpable and refuse to start on empty secrets.

Sessions run as the daemon's user, so ``/proc/<daemon pid>/environ`` (master key
included) was readable from any session shell. And a session's env carries the
namespace blanked to ``""``, which ``Settings`` reads as unset — so a blanked env would
boot an edge or daemon on the default SQLite store instead of failing.

The non-dumpable checks run in a CHILD process: the flag cannot be turned back on for
a process that is not root, and the test process must stay debuggable.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from server import startup_guard as sg
from viberpit import brand

NEW, OLD = brand.ENV_PREFIX, brand.LEGACY_ENV_PREFIX
REPO_ROOT = Path(__file__).resolve().parents[2]
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux")


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith((NEW, OLD))}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "src"),
                                         env.get("PYTHONPATH", "")])
    env.pop("INVOCATION_ID", None)
    env.pop("SYSTEMD_EXEC_PID", None)
    env.update(extra)
    return env


def _spawn_holder(code: str, **extra: str) -> subprocess.Popen:
    """A child that runs *code*, prints a line, then blocks on stdin. The line *code*
    printed before ``ready`` is kept on ``child.first_line``."""
    script = textwrap.dedent(code) + "\nprint('ready', flush=True)\nimport sys; sys.stdin.read()\n"
    child = subprocess.Popen([sys.executable, "-c", script], cwd=REPO_ROOT,
                             env=_child_env(SECRET_3386="hunter2", **extra),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    assert line.endswith("ready"), line
    child.first_line = line  # type: ignore[attr-defined]
    return child


def _environ_readable(pid: int) -> bool:
    try:
        Path(f"/proc/{pid}/environ").read_bytes()
        return True
    except PermissionError:
        return False


@linux_only
def test_a_non_dumpable_process_hides_its_environ_from_the_same_user():
    plain = _spawn_holder("pass")
    hardened = _spawn_holder(
        "from server.startup_guard import make_non_dumpable\n"
        "assert make_non_dumpable()")
    try:
        assert _environ_readable(plain.pid)          # the hole this closes
        assert not _environ_readable(hardened.pid)
    finally:
        for c in (plain, hardened):
            c.kill()
            c.wait()


@linux_only
def test_its_exec_children_stay_inspectable():
    """Process adoption reads a ``claude`` child's environ; the flag resets on exec."""
    parent = _spawn_holder("""
        import subprocess
        from server.startup_guard import make_non_dumpable
        make_non_dumpable()
        kid = subprocess.Popen(["sleep", "30"])
        print(kid.pid, end=" ")
    """)
    kid = int(parent.first_line.split()[0])  # type: ignore[attr-defined]
    try:
        assert not _environ_readable(parent.pid)
        assert _environ_readable(kid)
    finally:
        os.kill(kid, 9)
        parent.kill()
        parent.wait()


@linux_only
def test_importing_server_hardens_only_a_units_main_process():
    """``server/__init__.py`` is the edge's (uvicorn's) and the daemon's earliest hook.
    A child of a unit inherits ``INVOCATION_ID`` and ``SYSTEMD_EXEC_PID`` — a session's
    dev edge or pytest — but is not the unit's main process, so it stays dumpable
    (dev-server detection reads its ``/proc/<pid>/fd``)."""
    main = _spawn_holder("""
        import os
        os.environ["SYSTEMD_EXEC_PID"] = str(os.getpid())  # what systemd sets for ExecStart
        import server
    """, INVOCATION_ID="3386")
    inherited = _spawn_holder("import server", INVOCATION_ID="3386",
                              SYSTEMD_EXEC_PID=str(os.getpid()))
    by_hand = _spawn_holder("import server")
    try:
        assert not _environ_readable(main.pid)
        assert _environ_readable(inherited.pid)
        assert _environ_readable(by_hand.pid)
    finally:
        for c in (main, inherited, by_hand):
            c.kill()
            c.wait()


def test_harden_if_service_keys_on_the_units_main_pid(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(sg, "make_non_dumpable", lambda: calls.append(1) or True)
    assert sg.harden_if_service({"SYSTEMD_EXEC_PID": "41", "INVOCATION_ID": "x"}, pid=41) is True
    assert sg.harden_if_service({"SYSTEMD_EXEC_PID": "40", "INVOCATION_ID": "x"}, pid=41) is False
    assert sg.harden_if_service({"INVOCATION_ID": "x"}, pid=41) is False
    assert sg.harden_if_service({}, pid=41) is False
    assert calls == [1]


def test_make_non_dumpable_is_a_no_op_off_linux(monkeypatch):
    monkeypatch.setattr(sg.sys, "platform", "darwin")
    assert sg.make_non_dumpable() is False


# ── empty secrets ───────────────────────────────────────────────────────────


def test_empty_secret_names_are_the_secret_shaped_namespace_names_set_to_empty():
    env = {f"{NEW}MASTER_KEY": "", f"{OLD}DB_URL": "", f"{NEW}SESSION_SECRET": "",
           f"{NEW}GH_PROJECT_TOKEN": "ghp_real", f"{NEW}ENABLE_XVFB": "",
           f"{NEW}TAILNET_ALLOWLIST": "", "OTHER_TOKEN": ""}
    assert sg.empty_secret_names(env) == sorted(
        [f"{NEW}MASTER_KEY", f"{OLD}DB_URL", f"{NEW}SESSION_SECRET"])


def test_the_refusal_names_the_variables_and_never_a_value():
    env = {f"{NEW}DB_URL": "", f"{NEW}GH_PROJECT_TOKEN": "ghp_real"}
    with pytest.raises(sg.EmptySecretError) as err:
        sg.refuse_empty_secrets(env, process="edge")
    assert f"{NEW}DB_URL" in str(err.value) and "ghp_real" not in str(err.value)
    sg.refuse_empty_secrets({f"{NEW}DB_URL": "sqlite://"}, process="edge")


def test_the_edge_refuses_to_start_on_an_empty_secret():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    with tempfile.TemporaryDirectory() as d:
        env = {f"{NEW}DB_URL": f"sqlite+aiosqlite:///{Path(d) / 'g.db'}",
               f"{NEW}MASTER_KEY": ""}
        with patch.dict(os.environ, env):
            app = create_app(orchestrator=SessionOrchestrator(poll_interval=9999.0))
            with pytest.raises(sg.EmptySecretError, match=f"{NEW}MASTER_KEY"):
                with TestClient(app):
                    pass


def test_the_daemon_refuses_to_start_on_an_empty_secret(tmp_path):
    """Before it imports its modules or binds anything."""
    proc = subprocess.run(
        [sys.executable, "-m", "server.ipc", str(tmp_path / "d.sock")], cwd=REPO_ROOT,
        env=_child_env(**{f"{NEW}DB_URL": "", f"{NEW}MASTER_KEY": "",
                          "HOME": str(tmp_path)}),
        capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "EmptySecretError" in proc.stderr and f"{NEW}DB_URL" in proc.stderr
    assert not (tmp_path / "d.sock").exists()
