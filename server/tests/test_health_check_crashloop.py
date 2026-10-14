# server/tests/test_health_check_crashloop.py
"""#816: a non-converging daemon restart loop must escalate, not defer forever.

On 2026-08-29 rocky's daemon crash-looped 57 times (an ImportError off a 0-byte module)
while the health check correctly saw it unreachable and deliberately deferred to systemd
``Restart=always`` — which retries an identical failure forever. These tests run the
real script against PATH shims and drive ``NRestarts`` across successive runs, the way
the 60s timer does. The SSH shim executes the script's real probe against a separate
"daemon host" shim set, so the probe text itself is under test, not a canned reply.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "viberpit-health-check.sh"
REMOTE_HTTPS = "https://otherhost.taildead.ts.net:8737"
WEBHOOK = "https://ntfy.example.invalid/viberpit"


def _shim(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)


def _unit_shims(bindir: Path, knobs: Path, checkout: Path) -> None:
    """systemctl/git/journalctl for one host; NRestarts etc. are read from ``knobs``
    files at call time so a test can move them between runs."""
    _shim(bindir, "systemctl", f"""
k="{knobs}"
if [ "$1" = "is-active" ]; then
  [ "$(cat $k/active)" = active ] && exit 0 || exit 3
fi
if [ "$1" = "show" ]; then
  if [[ "$*" == *"-p NRestarts"* ]]; then
    echo "NRestarts=$(cat $k/nrestarts)"; echo "ActiveState=$(cat $k/active)"
    echo "Result=$(cat $k/result)"; echo "WorkingDirectory={checkout}"
    exit 0
  fi
  case "$*" in
    *LoadState*)        echo loaded ;;
    *WorkingDirectory*) echo "{checkout}" ;;
    *)                  echo '' ;;
  esac
  exit 0
fi
exit 0
""")
    _shim(bindir, "git", f'[ -f "{knobs}/dirty" ] && cat "{knobs}/dirty"; exit 0\n')
    _shim(bindir, "journalctl", """
echo "Started viberpit-daemon.service"
echo "ImportError: cannot import name 'SessionOrchestrator' from 'server.sessions.orchestrator'"
echo "viberpit-daemon.service: Main process exited, code=exited, status=1/FAILURE"
""")


class Host:
    """One test topology: this host (edge/watchdog) plus a remote daemon host."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state = tmp_path / "health-state"
        self.calls = tmp_path / "calls"
        self.calls.mkdir()
        self.local_bin, self.local = self._host("local")
        self.remote_bin, self.remote = self._host("remote")
        uds_ok = self.local / "uds-ok"
        remote_ok = self.remote / "https-ok"
        _shim(self.local_bin, "sudo", f'echo "$*" >> "{self.calls}/sudo"; exit 0\n')
        _shim(self.local_bin, "hostname", 'echo myhost\n')
        _shim(self.local_bin, "curl", f"""
echo "$*" >> "{self.calls}/curl"
url=""
for a in "$@"; do case "$a" in http://*|https://*) url="$a" ;; esac; done
case "$url" in
  http://daemon/healthz) [ -f "{uds_ok}" ] && {{ echo '{{"status":"ok"}}'; exit 0; }} ;;
  https://*/info) [ -f "{remote_ok}" ] && {{ echo '{{"status":"ok","protocol":3}}'; exit 0; }} ;;
esac
exit 7
""")
        # 5c's mount/DB probe gets canned healthy answers. The restart-loop probe (its
        # args are the daemon unit candidates) runs for real against the remote shims.
        _shim(self.local_bin, "ssh", f"""
echo "$*" >> "{self.calls}/ssh"
if [[ "$*" != *"daemon.service"* ]]; then
  cat >/dev/null; echo PG_OK
  for m in "$@"; do case "$m" in /*) echo "MOUNT_OK $m" ;; esac; done
  exit 0
fi
[ -f "{self.remote}/ssh-down" ] && exit 255
while [ "$1" != bash ]; do shift; done; shift 2  # ssh options + host, "bash -s"
PATH="{self.remote_bin}:/usr/bin:/bin" bash -s "$@"
""")
        _shim(self.local_bin, "health-python", 'cat >/dev/null; [ "${1:-}" = -c ] && echo 3; exit 0\n')
        _unit_shims(self.local_bin, self.local, self.tmp / "local-checkout")
        _unit_shims(self.remote_bin, self.remote, self.tmp / "remote-checkout")

    def _host(self, name: str) -> tuple[Path, Path]:
        bindir = self.tmp / f"{name}-bin"
        bindir.mkdir()
        knobs = self.tmp / f"{name}-knobs"
        knobs.mkdir()
        (self.tmp / f"{name}-checkout" / ".git").mkdir(parents=True)
        for k, v in {"nrestarts": "0", "active": "active", "result": "success"}.items():
            (knobs / k).write_text(v)
        return bindir, knobs

    def set(self, host: str, **knobs: str) -> None:
        d = self.local if host == "local" else self.remote
        for k, v in knobs.items():
            (d / k).write_text(v)

    def flag(self, host: str, name: str, on: bool) -> None:
        f = (self.local if host == "local" else self.remote) / name
        if on:
            f.write_text("1")
        else:
            f.unlink(missing_ok=True)

    def run(self, *, remote: bool) -> subprocess.CompletedProcess[str]:
        certs = self.tmp / "certs"
        certs.mkdir(exist_ok=True)
        for f in ("client.crt", "client.key", "ca.crt"):
            (certs / f).write_text("dummy\n")
        env = {
            "PATH": f"{self.local_bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "VIBERPIT_HEALTH_STATE_DIR": str(self.state),
            "VIBERPIT_LEDGER": str(self.tmp / "no-such-ledger.json"),
            "VIBERPIT_HEALTH_REPO_ROOT": str(self.tmp),
            "VIBERPIT_HEALTH_PYTHON": str(self.local_bin / "health-python"),
            "VIBERPIT_SESSIOND_SOCKET": str(self.tmp / "sessiond.sock"),
            "VIBERPIT_SESSIOND_CLIENT_CERT": str(certs / "client.crt"),
            "VIBERPIT_SESSIOND_CLIENT_KEY": str(certs / "client.key"),
            "VIBERPIT_SESSIOND_CLIENT_CA": str(certs / "ca.crt"),
            "VIBERPIT_ALERT_WEBHOOK": WEBHOOK,
            "VIBERPIT_PROBE_RETRIES": "1",
            "VIBERPIT_PROBE_TIMEOUT": "2",
            "VIBERPIT_DRAIN_WAIT": "1",
            "VIBERPIT_RESTART_COOLDOWN": "1",
            "VIBERPIT_STARTUP_GRACE": "0",
            "VIBERPIT_DISK_WARN_PCT": "101",
            "VIBERPIT_CRASHLOOP_RESTARTS": "5",
        }
        if remote:
            env["VIBERPIT_SESSIOND_ENDPOINT"] = REMOTE_HTTPS
            env["VIBERPIT_DAEMON_SSH_HOST"] = "otherhost"
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120
        )

    def webhook_posts(self) -> list[str]:
        f = self.calls / "curl"
        lines = f.read_text().splitlines() if f.exists() else []
        return [ln for ln in lines if WEBHOOK in ln]


def _remote_down_edge(tmp_path: Path) -> Host:
    """dobby-edge shape: no local daemon (inactive, counter static), remote unreachable."""
    h = Host(tmp_path)
    h.set("local", active="inactive", result="success")
    h.set("remote", active="activating", result="exit-code", nrestarts="10")
    return h


def test_remote_restart_loop_escalates_once_nrestarts_climbs(tmp_path: Path) -> None:
    h = _remote_down_edge(tmp_path)
    # Six files zeroed by a hard reset, as `git status --porcelain` lists them.
    h.set("remote", dirty="".join(f" M server/sessions/m{i}.py\n" for i in range(6)))

    first = h.run(remote=True)
    assert "ALERT[remote-daemon-unreachable]" in first.stdout
    assert "ALERT[daemon-crashloop" not in first.stdout, "first sighting only baselines"

    h.set("remote", nrestarts="14")
    second = h.run(remote=True)
    assert "ALERT[daemon-crashloop" not in second.stdout, "4 restarts is under the threshold"

    h.set("remote", nrestarts="15")
    third = h.run(remote=True)
    assert "ALERT[daemon-crashloop-remote]" in third.stdout, third.stdout
    line = next(ln for ln in third.stdout.splitlines() if "ALERT[daemon-crashloop-remote]" in ln)
    assert "otherhost" in line and "NRestarts=15" in line and "restarted 5 time" in line
    assert "6 uncommitted change" in line, "dirty-checkout diagnostic must be in the alert"
    assert "ImportError: cannot import name 'SessionOrchestrator'" in line
    assert "ALERT[daemon-crashloop-local]" not in third.stdout
    # Escalation, never repair: nothing restarted or mutated on either host.
    assert not (h.calls / "sudo").exists()
    assert any("Priority: urgent" in p for p in h.webhook_posts()), h.webhook_posts()


def test_unreachable_without_restarts_is_not_a_restart_loop(tmp_path: Path) -> None:
    """A host that is down, or a daemon that is merely wedged, has a static counter —
    still the plain unreachable alert, not the restart-loop one."""
    h = _remote_down_edge(tmp_path)
    for _ in range(3):
        proc = h.run(remote=True)
        assert "ALERT[remote-daemon-unreachable]" in proc.stdout
        assert "ALERT[daemon-crashloop" not in proc.stdout


def test_unreadable_remote_counter_is_skipped_not_alerted(tmp_path: Path) -> None:
    h = _remote_down_edge(tmp_path)
    h.flag("remote", "ssh-down", True)
    proc = h.run(remote=True)
    assert "restart-loop check (otherhost) skipped" in proc.stdout
    assert "ALERT[daemon-crashloop" not in proc.stdout


def test_recovery_clears_the_baseline_and_alert_state(tmp_path: Path) -> None:
    h = _remote_down_edge(tmp_path)
    h.run(remote=True)
    h.set("remote", nrestarts="30")
    assert "ALERT[daemon-crashloop-remote]" in h.run(remote=True).stdout
    assert (h.state / "alert-daemon-crashloop-remote").exists()

    h.flag("remote", "https-ok", True)
    healthy = h.run(remote=True)
    assert "ALERT[" not in healthy.stdout, healthy.stdout
    assert not (h.state / "alert-daemon-crashloop-remote").exists()
    assert not (h.state / "crashloop-baseline-remote").exists()

    # A later, unrelated outage baselines afresh rather than counting the old restarts.
    h.flag("remote", "https-ok", False)
    h.set("remote", nrestarts="31")
    assert "ALERT[daemon-crashloop" not in h.run(remote=True).stdout


def test_counter_reset_by_a_manual_start_rebaselines(tmp_path: Path) -> None:
    h = _remote_down_edge(tmp_path)
    h.run(remote=True)                     # baseline 10
    h.set("remote", nrestarts="2")         # someone ran `systemctl restart`
    assert "ALERT[daemon-crashloop" not in h.run(remote=True).stdout
    h.set("remote", nrestarts="6")         # 4 since the reset: still under 5
    assert "ALERT[daemon-crashloop" not in h.run(remote=True).stdout
    h.set("remote", nrestarts="7")
    assert "ALERT[daemon-crashloop-remote]" in h.run(remote=True).stdout


def test_start_limit_hit_escalates_immediately(tmp_path: Path) -> None:
    """systemd's own StartLimitBurst give-up is the same verdict, already reached."""
    h = _remote_down_edge(tmp_path)
    h.set("remote", active="failed", result="start-limit-hit")
    proc = h.run(remote=True)
    assert "ALERT[daemon-crashloop-remote]" in proc.stdout
    assert "state=failed/start-limit-hit" in proc.stdout


def test_local_restart_loop_escalates_instead_of_not_our_job(tmp_path: Path) -> None:
    h = Host(tmp_path)
    h.set("local", active="activating", result="exit-code", nrestarts="3")
    first = h.run(remote=False)
    assert "not our job" in first.stdout
    assert "ALERT[daemon-crashloop" not in first.stdout

    h.set("local", nrestarts="9")
    second = h.run(remote=False)
    assert "ALERT[daemon-crashloop-local]" in second.stdout, second.stdout
    assert "on myhost" in second.stdout
    assert not (h.calls / "sudo").exists(), "escalate only — never restart/repair"
    assert not (h.calls / "ssh").exists(), "local mode never reaches for SSH"


def test_healthy_local_daemon_never_baselines(tmp_path: Path) -> None:
    h = Host(tmp_path)
    h.flag("local", "uds-ok", True)
    h.set("local", nrestarts="40")
    proc = h.run(remote=False)
    assert "ALERT[" not in proc.stdout, proc.stdout
    assert not (h.state / "crashloop-baseline-local").exists()
