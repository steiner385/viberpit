"""Behavioral spec for deploy/watchdog/test-ssh.sh.

This is the loopback half of the hardware-watchdog test pair (the outward half is
pinned by ``test_watchdog_reachability.py``). It proves sshd is not merely
*listening* but actually *speaking SSH*, by reading the protocol version-exchange
greeting and requiring it to start with ``SSH-``.

**A non-zero exit from this script reboots the physical host.** So the property
under test is asymmetric and the fail-safe direction is the important one: an
inconclusive result MUST exit 0, and only a clear, positively-verified "sshd is
not answering" may exit non-zero. Those guards ARE the design, and each is pinned
here.

The script is driven as a subprocess against stub listeners bound to ephemeral
loopback ports, with every knob injected via ``WDS_*`` — no real sshd, and no
dependency on port 22 being up (or down) on the machine running the suite.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "watchdog" / "test-ssh.sh"

# The `watchdog.conf` contract this script must live inside. If test-timeout ever
# drops below the script's worst-case runtime the daemon SIGKILLs it mid-run.
TEST_TIMEOUT = 30


@contextmanager
def listener(greeting: bytes | None) -> Iterator[int]:
    """Serve `greeting` to every connection on an ephemeral loopback port.

    ``greeting=None`` accepts the connection and then says nothing at all — the
    "wedged sshd" case that a bare TCP-connect check cannot distinguish from a
    healthy one.
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with closing(conn):
                if greeting is not None:
                    try:
                        conn.sendall(greeting)
                    except OSError:
                        pass
                # Hold the connection open briefly so the client controls the close.
                stop.wait(0.05)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=3)
        srv.close()


def closed_port() -> int:
    """Return a loopback port with nothing listening on it."""
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(
    *,
    port: int,
    probe_port: int | None = None,
    attempts: int = 3,
    read_timeout: str = "1",
    connect_timeout: str = "3",
) -> subprocess.CompletedProcess[str]:
    """Drive the script against `port`, with retry sleep collapsed for speed."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "WDS_PORT": str(port),
        "WDS_ATTEMPTS": str(attempts),
        "WDS_READ_TIMEOUT": read_timeout,
        "WDS_CONNECT_TIMEOUT": connect_timeout,
        "WDS_RETRY_SLEEP": "0",
        # Default self-test probe port is 1; tests that care override it.
        "WDS_PROBE_PORT": str(probe_port if probe_port is not None else closed_port()),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_is_executable_and_syntactically_valid() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & 0o111, "script must be executable (watchdog only runs executables)"
    syntax = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_healthy_sshd_passes_silently() -> None:
    """The happy path: a real SSH greeting exits 0 and logs nothing."""
    with listener(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13\r\n") as port:
        result = run(port=port)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("repeat", range(10))
def test_healthy_sshd_passes_every_single_run(repeat: int) -> None:
    """Flakiness here reboots a production host, so the happy path is hammered.

    A one-in-N spurious non-zero is not a test nuisance; it is an outage.
    """
    with listener(b"SSH-2.0-OpenSSH_9.6p1\r\n") as port:
        assert run(port=port).returncode == 0


def test_sshd_not_listening_fails() -> None:
    """The genuine failure the watchdog exists to catch: nothing on the port."""
    result = run(port=closed_port())
    assert result.returncode == 1


def test_wedged_sshd_fails() -> None:
    """Accepts TCP but never greets — the case a bare connect check would PASS.

    This is defect #2 of the script this replaced: `echo > /dev/tcp/...` proved
    only that the port accepted a connection.
    """
    with listener(None) as port:
        result = run(port=port)
    assert result.returncode == 1


def test_non_ssh_greeting_fails() -> None:
    """Something is listening on 22, but it is not sshd."""
    with listener(b"HTTP/1.1 400 Bad Request\r\n") as port:
        result = run(port=port)
    assert result.returncode == 1


def test_passes_when_dev_tcp_support_is_unproven() -> None:
    """THE fail-safe guard: an unusable probe mechanism must never reboot the host.

    The script only trusts a failed connect once it has confirmed this bash can
    actually open /dev/tcp (a closed port must answer "Connection refused"). Here
    the self-test port is OPEN, so support is unproven — the verdict is
    inconclusive and the script must pass rather than reboot the machine.
    """
    with listener(b"SSH-2.0-OpenSSH_9.6p1\r\n") as open_port:
        result = run(port=closed_port(), probe_port=open_port)
    assert result.returncode == 0, "inconclusive tooling must fail SAFE (exit 0)"


def test_transients_are_absorbed_by_retries() -> None:
    """A single failed probe must not fail the test; all attempts must fail."""
    with listener(b"SSH-2.0-OpenSSH_9.6p1\r\n") as port:
        # One attempt against a healthy listener still passes...
        assert run(port=port, attempts=1).returncode == 0
    # ...and the default is >1 so a lone transient can never trip a reboot.
    text = SCRIPT.read_text()
    assert 'WDS_ATTEMPTS:-3' in text, "default attempts must stay >1 to absorb transients"


def test_worst_case_runtime_stays_under_watchdog_test_timeout() -> None:
    """The daemon SIGKILLs a test that outruns `test-timeout = 30`.

    Measured against the slowest path: every attempt connects but never greets,
    so each one burns the full read timeout, plus the self-test at the end.
    """
    with listener(None) as port:
        started = time.monotonic()
        result = run(port=port, read_timeout="3", connect_timeout="4")
        elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert elapsed < TEST_TIMEOUT, f"worst case {elapsed:.1f}s exceeds test-timeout={TEST_TIMEOUT}s"


def test_never_attempts_authentication() -> None:
    """Defect #1 of the replaced script: it ssh'd to localhost as root every 10s.

    Root cannot authenticate, so each run logged an sshd auth failure — ~8,700 a
    day of pure noise. The check must stay authentication-free.
    """
    body = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    # Word-boundary match so the `watchdog-ssh` logger tag is not a false positive.
    assert re.search(r"(?<![\w-])ssh\s", body) is None, "must not invoke the ssh client"
    assert "BatchMode" not in body


def test_does_not_use_set_e() -> None:
    """`set -e` would let a stray non-zero from any intermediate command become
    the script's exit status — and this script's exit status reboots the host."""
    body = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "set -e" not in body
