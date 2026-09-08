# server/tests/test_health_check_fleet.py
"""Regression coverage for #839: the health-check script predates the fleet.

Found during the 2026-08-30 dobby-primary cutover. ``scripts/ccswitch-health-check.sh``
was written for two topologies only — daemon local-on-UDS, or daemon remote-over-mTLS —
and a LOCAL daemon that binds https (a registered fleet runner, #807) broke both of its
core checks:

* The wedge watchdog (section 1) probed the local UDS unconditionally. A fleet-runner
  daemon binds https and never creates the UDS, so the probe failed forever and the
  watchdog restarted a perfectly healthy daemon every ``RESTART_COOLDOWN``.
* The split-brain guard (5e) alerted whenever an https endpoint was configured AND the
  local daemon unit was active — which is exactly what a deliberate local fleet runner
  looks like. It must distinguish "endpoint points at THIS host" and "local daemon is a
  registered+enabled fleet host" from the genuine two-orchestrators accident.
* The remote mount/DB checks (5c/5d) SSH'd to a "remote" daemon host that no longer
  exists in this topology.

Like ``test_mounts_unit.py``, these tests execute the real script against PATH shims
(``systemctl``/``curl``/``ssh``/``sudo``/``hostname``) rather than grepping it — a probe
that targets the wrong transport still passes a grep.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ccswitch-health-check.sh"

TAILNET_NAME = "myhost.taildead.ts.net"
LOCAL_HTTPS = f"https://{TAILNET_NAME}:8737"
REMOTE_HTTPS = "https://otherhost.taildead.ts.net:8737"


def _write_shim(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)


def _make_shims(
    tmp_path: Path,
    *,
    unit_active: bool,
    unit_environment: str = "",
    unit_env_file: Path | None = None,
    uds_healthz_ok: bool = False,
    https_healthz_ok: bool = False,
    registry_endpoints: str = "",
    unit_main_pid: int = 0,
    config_counts: str = "0 0",
) -> tuple[Path, Path]:
    """Build the shim bin dir + state dir the script runs against.

    ``unit_environment`` is what ``systemctl show -p Environment --value`` reports for
    the daemon unit (its drop-in Environment= lines); ``unit_env_file`` simulates an
    EnvironmentFile= the unit sources. ``registry_endpoints`` is what the fleet hosts
    registry query prints (one enabled-host endpoint per line).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "shim-state"
    state.mkdir()

    env_files_line = f"{unit_env_file} (ignore_errors=yes)" if unit_env_file else ""
    _write_shim(bindir, "systemctl", f"""
if [ "$1" = "is-active" ]; then
  exit {0 if unit_active else 3}
fi
if [ "$1" = "show" ]; then
  case "$*" in
    *Environment\\ --value*) echo '{unit_environment}' ;;
    *EnvironmentFiles*)      echo '{env_files_line}' ;;
    *MainPID*)               echo '{unit_main_pid}' ;;
    *)                       echo '' ;;
  esac
  exit 0
fi
exit 0
""")
    _write_shim(bindir, "sudo", f"""
echo "$*" >> "{state}/sudo-calls"
exit 0
""")
    _write_shim(bindir, "ssh", f"""
echo "$*" >> "{state}/ssh-calls"
exit 255
""")
    _write_shim(bindir, "hostname", """
case "${1:-}" in
  -f) echo "%s" ;;
  *)  echo "myhost" ;;
esac
""" % TAILNET_NAME)
    _write_shim(bindir, "curl", f"""
echo "$*" >> "{state}/curl-calls"
url=""
for a in "$@"; do case "$a" in http://*|https://*) url="$a" ;; esac; done
if [[ "$*" == *"--unix-socket"* ]]; then
  case "$url" in
    *healthz*) [ "{1 if uds_healthz_ok else 0}" = 1 ] && {{ echo '{{"status":"ok"}}'; exit 0; }} || exit 7 ;;
    *) exit 7 ;;
  esac
fi
case "$url" in
  https://*/healthz) [ "{1 if https_healthz_ok else 0}" = 1 ] && {{ echo '{{"status":"ok"}}'; exit 0; }} || exit 7 ;;
  https://*/info)    [ "{1 if https_healthz_ok else 0}" = 1 ] && {{ echo '{{"status":"ok","protocol":3}}'; exit 0; }} || exit 7 ;;
  *) exit 7 ;;
esac
""")
    # Stands in for the repo venv python. `-c` reads this build's PROTOCOL_VERSION; a
    # heredoc on stdin is one of two queries, told apart by what it imports: the fleet
    # registry (one enabled endpoint per line) or the #1005 config-coherence counters
    # ("<torn reads> <corruption artifacts>").
    _write_shim(bindir, "health-python", f"""
if [ "${{1:-}}" = "-c" ]; then echo 3; exit 0; fi
stdin=$(cat)
if [[ "$stdin" == *scratch_config* ]]; then
  printf '%s' '{config_counts}'
  exit 0
fi
printf '%s' '{registry_endpoints}'
""")
    return bindir, state


def _run(
    tmp_path: Path,
    bindir: Path,
    *,
    script_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    certs = tmp_path / "certs"
    certs.mkdir(exist_ok=True)
    for f in ("client.crt", "client.key", "ca.crt"):
        (certs / f).write_text("dummy\n")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CCSWITCH_HEALTH_STATE_DIR": str(tmp_path / "health-state"),
        "CCSWITCH_LEDGER": str(tmp_path / "no-such-ledger.json"),
        "CCSWITCH_HEALTH_REPO_ROOT": str(tmp_path),
        "CCSWITCH_HEALTH_PYTHON": str(bindir / "health-python"),
        "CCSWITCH_SESSIOND_CLIENT_CERT": str(certs / "client.crt"),
        "CCSWITCH_SESSIOND_CLIENT_KEY": str(certs / "client.key"),
        "CCSWITCH_SESSIOND_CLIENT_CA": str(certs / "ca.crt"),
        # Keep failure-path retries/sleeps short so tests stay fast.
        "CCSWITCH_PROBE_RETRIES": "1",
        "CCSWITCH_PROBE_TIMEOUT": "2",
        "CCSWITCH_DRAIN_WAIT": "1",
        "CCSWITCH_RESTART_COOLDOWN": "1",
        "CCSWITCH_STARTUP_GRACE": "0",
        "CCSWITCH_DISK_WARN_PCT": "101",
        **script_env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120
    )


def _calls(state: Path, name: str) -> str:
    f = state / name
    return f.read_text() if f.exists() else ""


def test_local_https_fleet_runner_is_not_split_brain(tmp_path: Path) -> None:
    """Endpoint pointing at THIS host + active local daemon is the deliberate
    fleet-runner topology, not the two-orchestrators accident — no alert, no restart."""
    bindir, state = _make_shims(
        tmp_path,
        unit_active=True,
        unit_environment=f"CCSWITCH_SESSIOND_ENDPOINT={LOCAL_HTTPS}",
        https_healthz_ok=True,
    )
    proc = _run(tmp_path, bindir, script_env={"CCSWITCH_SESSIOND_ENDPOINT": LOCAL_HTTPS})
    assert "ALERT[split-brain]" not in proc.stdout
    assert _calls(state, "sudo-calls") == "", "healthy fleet runner must not be restarted"


def test_wedge_probe_targets_the_daemon_https_bind_not_the_uds(tmp_path: Path) -> None:
    """A fleet-runner daemon binds https and never creates the UDS. The watchdog must
    probe what the daemon's own unit env says it binds (here via its EnvironmentFile=),
    or it restarts a healthy daemon forever."""
    env_file = tmp_path / "daemon.env"
    env_file.write_text(f'CCSWITCH_SESSIOND_ENDPOINT="{LOCAL_HTTPS}"\n')
    bindir, state = _make_shims(
        tmp_path, unit_active=True, unit_env_file=env_file, https_healthz_ok=True
    )
    proc = _run(tmp_path, bindir, script_env={"CCSWITCH_SESSIOND_ENDPOINT": LOCAL_HTTPS})
    curl_calls = _calls(state, "curl-calls")
    assert f"{LOCAL_HTTPS}/healthz" in curl_calls, "watchdog must probe the https bind"
    assert "--unix-socket" not in curl_calls, "no UDS exists for an https-bound daemon"
    assert _calls(state, "sudo-calls") == "", proc.stdout


def test_wedge_restart_still_fires_when_the_https_daemon_is_unresponsive(
    tmp_path: Path,
) -> None:
    """The fix must not blind the watchdog: an active https daemon whose control plane
    stops answering is still the wedge, and still gets restarted."""
    bindir, state = _make_shims(
        tmp_path,
        unit_active=True,
        unit_environment=f"CCSWITCH_SESSIOND_ENDPOINT={LOCAL_HTTPS}",
        https_healthz_ok=False,
    )
    proc = _run(tmp_path, bindir, script_env={"CCSWITCH_SESSIOND_ENDPOINT": LOCAL_HTTPS})
    assert "systemctl restart viberpit-daemon.service" in _calls(state, "sudo-calls")
    assert "ALERT[daemon-wedged]" in proc.stdout


def test_wedged_daemon_that_ignores_sigterm_is_sigkilled_after_drain_wait(
    tmp_path: Path,
) -> None:
    """The daemon drains its own turns on SIGTERM, which is why the unit's
    TimeoutStopSec is 660s — but a WEDGED loop never runs that drain, and the watchdog
    must not sit out 11 minutes on a daemon it already knows is dead. Old MainPID still
    alive after DRAIN_WAIT → SIGKILL the unit."""
    import subprocess as sp
    stuck = sp.Popen(["sleep", "300"])
    try:
        bindir, state = _make_shims(
            tmp_path, unit_active=True, uds_healthz_ok=False, unit_main_pid=stuck.pid
        )
        proc = _run(tmp_path, bindir, script_env={})
        sudo_calls = _calls(state, "sudo-calls")
        assert "systemctl restart viberpit-daemon.service" in sudo_calls
        assert "systemctl kill -s SIGKILL viberpit-daemon.service" in sudo_calls, proc.stdout
        assert "SIGKILLing the unit" in proc.stdout
        assert "ALERT[daemon-wedged]" in proc.stdout
    finally:
        stuck.kill()
        stuck.wait()


def test_daemon_that_exits_on_sigterm_is_not_sigkilled(tmp_path: Path) -> None:
    """A slow-but-alive daemon drains and exits by itself; the escalation must stay
    holstered so its in-flight turns survive."""
    import subprocess as sp
    gone = sp.Popen(["true"])
    gone.wait()                       # reaped: kill -0 on this pid fails
    bindir, state = _make_shims(
        tmp_path, unit_active=True, uds_healthz_ok=False, unit_main_pid=gone.pid
    )
    proc = _run(tmp_path, bindir, script_env={})
    sudo_calls = _calls(state, "sudo-calls")
    assert "systemctl restart viberpit-daemon.service" in sudo_calls
    assert "SIGKILL" not in sudo_calls, proc.stdout
    assert "ALERT[daemon-wedged]" in proc.stdout


def test_uds_daemon_wedge_behavior_unchanged(tmp_path: Path) -> None:
    """Pre-fleet local topology (no https endpoint anywhere): the watchdog still probes
    the UDS and still restarts an active-but-unresponsive daemon."""
    bindir, state = _make_shims(tmp_path, unit_active=True, uds_healthz_ok=False)
    proc = _run(tmp_path, bindir, script_env={})
    assert "--unix-socket" in _calls(state, "curl-calls")
    assert "systemctl restart viberpit-daemon.service" in _calls(state, "sudo-calls")
    assert "ALERT[daemon-wedged]" in proc.stdout


def test_remote_endpoint_with_unregistered_local_daemon_still_alerts(
    tmp_path: Path,
) -> None:
    """The guard keeps its teeth: endpoint at ANOTHER host + an active local daemon that
    is NOT in the fleet registry is the 2026-06-23 incident shape and must alert."""
    bindir, _ = _make_shims(
        tmp_path,
        unit_active=True,
        uds_healthz_ok=True,
        https_healthz_ok=True,
        registry_endpoints="",
    )
    proc = _run(
        tmp_path,
        bindir,
        script_env={
            "CCSWITCH_SESSIOND_ENDPOINT": REMOTE_HTTPS,
            "CCSWITCH_DB_URL": "sqlite+aiosqlite:///unused.db",
        },
    )
    assert "ALERT[split-brain]" in proc.stdout


def test_registered_enabled_fleet_host_suppresses_split_brain(tmp_path: Path) -> None:
    """A local daemon that the fleet registry lists as an enabled host (endpoint
    resolving to this host) is a deliberate fleet runner even when the edge's primary
    endpoint points elsewhere — not split-brain."""
    bindir, _ = _make_shims(
        tmp_path,
        unit_active=True,
        uds_healthz_ok=True,
        https_healthz_ok=True,
        registry_endpoints=f"{LOCAL_HTTPS}\n",
    )
    proc = _run(
        tmp_path,
        bindir,
        script_env={
            "CCSWITCH_SESSIOND_ENDPOINT": REMOTE_HTTPS,
            "CCSWITCH_DB_URL": "sqlite+aiosqlite:///unused.db",
        },
    )
    assert "ALERT[split-brain]" not in proc.stdout


def test_local_daemon_skips_ssh_mount_checks(tmp_path: Path) -> None:
    """5c/5d assume a remote daemon host with sshfs mounts. With the daemon local there
    is no SSH hop and no sshfs to go stale — the script must not SSH anywhere (the old
    CCSWITCH_DAEMON_SSH_HOST may still point at a decommissioned host) nor emit mount
    alerts for what are now plain local directories."""
    bindir, state = _make_shims(
        tmp_path,
        unit_active=True,
        unit_environment=f"CCSWITCH_SESSIOND_ENDPOINT={LOCAL_HTTPS}",
        https_healthz_ok=True,
    )
    # A local listener stands in for Postgres so the local DB probe passes.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        proc = _run(
            tmp_path,
            bindir,
            script_env={
                "CCSWITCH_SESSIOND_ENDPOINT": LOCAL_HTTPS,
                "CCSWITCH_DAEMON_SSH_HOST": "decommissioned-host",
                "CCSWITCH_HEALTH_REMOTE_PG_HOST": "127.0.0.1",
                "CCSWITCH_HEALTH_REMOTE_PG_PORT": str(port),
            },
        )
    finally:
        listener.close()
    assert _calls(state, "ssh-calls") == "", "no SSH hop exists for a local daemon"
    assert "ALERT[mount-" not in proc.stdout
    assert "ALERT[remote-ssh-unreachable]" not in proc.stdout
    assert "ALERT[remote-db-unreachable]" not in proc.stdout


def test_local_daemon_db_check_runs_locally(tmp_path: Path) -> None:
    """The DB probe survives the topology change: with a local daemon it runs from this
    host directly (no SSH) and still alerts when Postgres is unreachable."""
    bindir, state = _make_shims(
        tmp_path,
        unit_active=True,
        unit_environment=f"CCSWITCH_SESSIOND_ENDPOINT={LOCAL_HTTPS}",
        https_healthz_ok=True,
    )
    # An ephemeral port we bound and immediately closed — guaranteed refused.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    proc = _run(
        tmp_path,
        bindir,
        script_env={
            "CCSWITCH_SESSIOND_ENDPOINT": LOCAL_HTTPS,
            "CCSWITCH_DAEMON_SSH_HOST": "decommissioned-host",
            "CCSWITCH_HEALTH_REMOTE_PG_HOST": "127.0.0.1",
            "CCSWITCH_HEALTH_REMOTE_PG_PORT": str(dead_port),
        },
    )
    assert _calls(state, "ssh-calls") == ""
    assert "ALERT[remote-db-unreachable]" in proc.stdout


def test_split_brain_message_still_documented_for_operators() -> None:
    """The script's own header must describe the fleet-aware guard, not the pre-fleet
    'https endpoint + active local unit == split-brain' rule that cried wolf (#839)."""
    text = SCRIPT.read_text()
    assert "registered" in text and "fleet" in text, (
        "header should explain the registered-fleet-host / endpoint-is-this-host carve-outs"
    )


# --- #1005: shared-config coherence alerting ------------------------------------------
#
# rocky reset the fleet's shared .claude.json ~1,100x/day for three days and nothing
# surfaced it: the daemon logged "is corrupted: JSON Parse error" to its own journal, and
# the only on-disk evidence was rmtree'd by the next compose(). Section 6 turns both
# counters into an alert, so the next occurrence is loud on the first hour, not the
# third day.


def _quiet_local_daemon_env(tmp_path: Path) -> dict[str, str]:
    """Script env with sections 1-5 inert, so a test isolates section 6."""
    return {"CCSWITCH_MEM_WARN_BYTES": "999999999999"}


def test_torn_reads_on_this_host_raise_an_alert(tmp_path):
    bindir, _ = _make_shims(tmp_path, unit_active=True, uds_healthz_ok=True,
                            config_counts="7 0")
    proc = _run(tmp_path, bindir, script_env=_quiet_local_daemon_env(tmp_path))
    assert "ALERT[config-torn-read]" in proc.stdout, proc.stdout
    assert "7 torn read(s)" in proc.stdout


def test_corruption_artifacts_alert_even_when_this_host_saw_no_torn_reads(tmp_path):
    """The cross-host case: some OTHER host still has the cached mount profile.

    This host's scratch-config layer is clean, but the shared store keeps gaining
    `.claude.json.corrupted.*` artifacts — the fleet is still resetting the config.
    """
    bindir, _ = _make_shims(tmp_path, unit_active=True, uds_healthz_ok=True,
                            config_counts="0 12")
    proc = _run(tmp_path, bindir, script_env=_quiet_local_daemon_env(tmp_path))
    assert "ALERT[config-torn-read]" in proc.stdout, proc.stdout
    assert "12 '.claude.json.corrupted.*' reset(s)" in proc.stdout


def test_a_coherent_fleet_is_silent(tmp_path):
    """The post-fix steady state — and what `verify` in #1005 asks us to be able to see."""
    bindir, _ = _make_shims(tmp_path, unit_active=True, uds_healthz_ok=True,
                            config_counts="0 0")
    proc = _run(tmp_path, bindir, script_env=_quiet_local_daemon_env(tmp_path))
    assert "ALERT[config-torn-read]" not in proc.stdout, proc.stdout


def test_unreadable_counters_degrade_to_a_log_not_a_false_alert(tmp_path):
    """A stale checkout / broken import must not page anyone."""
    bindir, _ = _make_shims(tmp_path, unit_active=True, uds_healthz_ok=True,
                            config_counts="Traceback (most recent call last)")
    proc = _run(tmp_path, bindir, script_env=_quiet_local_daemon_env(tmp_path))
    assert "ALERT[config-torn-read]" not in proc.stdout
    assert "config coherence counters unavailable" in proc.stdout


def test_the_alert_names_the_mount_option_that_fixes_it(tmp_path):
    """An alert that does not say what to do is a log line. Point at the actual knob."""
    bindir, _ = _make_shims(tmp_path, unit_active=True, uds_healthz_ok=True,
                            config_counts="1 1")
    proc = _run(tmp_path, bindir, script_env=_quiet_local_daemon_env(tmp_path))
    assert "attr_timeout=0" in proc.stdout
    assert "bin-ccswitch-mounts.sh" in proc.stdout
