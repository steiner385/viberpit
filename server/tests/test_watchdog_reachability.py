"""Behavioral spec for deploy/watchdog/test-reachability.sh.

This hardware-watchdog test script is the fix for issue #767: rocky's watchdog
could only prove sshd was alive *on the box* (loopback), so an "alive but
unreachable" host never triggered a reset. The replacement probes OUTWARD, which
is exactly what can reboot-loop a whole network if the guards are wrong — so the
guards ARE the design and every one of them is pinned here.

The script is driven as a subprocess with all non-determinism injected:
  * a PATH-stubbed ``ping`` whose success set is ``FAKE_PING_OK``
  * ``WDR_NOW`` / ``WDR_UPTIME_S`` for the clock and boot age
  * ``WDR_STATE_DIR`` pointing at a tmp dir
so no real network, real clock, or real /proc is touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "watchdog" / "test-reachability.sh"

# Defaults the script ships with; the tests use these so a change to a default
# that would weaken a guard shows up as a failure here.
BOOT_GRACE = 600
FAIL_WINDOW = 300
REBOOT_COOLDOWN = 1800


def _fake_ping(bin_dir: Path) -> None:
    """Install a ``ping`` stub: exit 0 iff the target (last arg) is in FAKE_PING_OK."""
    ping = bin_dir / "ping"
    ping.write_text(
        "#!/usr/bin/env bash\n"
        'host="${@: -1}"\n'
        "for ok in $FAKE_PING_OK; do [ \"$ok\" = \"$host\" ] && exit 0; done\n"
        "exit 1\n"
    )
    ping.chmod(0o755)


def run(
    tmp_path: Path,
    *,
    arg: str = "test",
    targets: str = "gw dobby extra",
    ping_ok: str = "",
    now: int = 1_000_000,
    uptime: int = 10_000,
    state_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_ping(bin_dir)
    sd = state_dir if state_dir is not None else (tmp_path / "state")
    # Hermetic package-manager view: a clean dpkg dir and no held locks, unless a test
    # dirties them, so results never depend on whether the CI box is running apt.
    dpkg_dir = _dpkg_dir(tmp_path)
    proc_locks = tmp_path / "proc-locks"
    if not proc_locks.exists():
        proc_locks.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FAKE_PING_OK": ping_ok,
        "WDR_CONF": str(tmp_path / "does-not-exist.conf"),
        "WDR_TARGETS": targets,
        "WDR_STATE_DIR": str(sd),
        "WDR_NOW": str(now),
        "WDR_UPTIME_S": str(uptime),
        "WDR_BOOT_GRACE": str(BOOT_GRACE),
        "WDR_FAIL_WINDOW": str(FAIL_WINDOW),
        "WDR_REBOOT_COOLDOWN": str(REBOOT_COOLDOWN),
        "WDR_PING_COUNT": "1",
        "WDR_PING_TIMEOUT": "1",
        "WDR_DPKG_DIR": str(dpkg_dir),
        "WDR_PROC_LOCKS": str(proc_locks),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), arg],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _dpkg_dir(tmp_path: Path) -> Path:
    """A fake /var/lib/dpkg: settled by default (every package installed, empty journal)."""
    d = tmp_path / "dpkg"
    if not d.exists():
        (d / "updates").mkdir(parents=True)
        (d / "lock-frontend").write_text("")
        (d / "status").write_text(
            "Package: bash\nStatus: install ok installed\n\n"
            "Package: oldpkg\nStatus: deinstall ok config-files\n\n"
        )
    return d


def _hold_dpkg_lock(tmp_path: Path) -> None:
    """Make /proc/locks show a POSIX write lock on the fake lock-frontend (apt running)."""
    inode = (_dpkg_dir(tmp_path) / "lock-frontend").stat().st_ino
    (tmp_path / "proc-locks").write_text(
        f"1: POSIX  ADVISORY  WRITE 4242 08:02:{inode} 0 EOF\n"
        f"2: FLOCK  ADVISORY  WRITE 99 08:02:{inode + 1} 0 EOF\n"
    )


def _first_failure(state_dir: Path) -> Path:
    return state_dir / "first-failure"


def _marker(state_dir: Path) -> Path:
    return state_dir / "last-reboot"


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} must be executable"


def test_no_targets_is_a_noop(tmp_path: Path) -> None:
    # Safest possible default: an unconfigured host must NEVER reboot itself.
    r = run(tmp_path, targets="", ping_ok="")
    assert r.returncode == 0, r.stderr
    assert "no targets" in (r.stdout + r.stderr).lower()


def test_all_targets_reachable_is_healthy(tmp_path: Path) -> None:
    r = run(tmp_path, ping_ok="gw dobby extra")
    assert r.returncode == 0, r.stderr
    assert not _first_failure(tmp_path / "state").exists()


def test_one_reachable_target_is_healthy(tmp_path: Path) -> None:
    # "requiring ALL to fail" — a single live peer keeps the host healthy.
    r = run(tmp_path, ping_ok="dobby")
    assert r.returncode == 0, r.stderr
    assert not _first_failure(tmp_path / "state").exists()


def test_first_all_down_records_window_but_does_not_reboot(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    r = run(tmp_path, ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode == 0, r.stderr  # sustained window not yet elapsed
    assert _first_failure(sd).read_text().strip() == "1000000"


def test_sustained_outage_triggers_reboot(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    sd.mkdir()
    # First failure was seen a full window+1 ago and never cleared.
    _first_failure(sd).write_text(str(1_000_000 - FAIL_WINDOW - 1))
    r = run(tmp_path, ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode != 0, "sustained all-target outage must request a reboot"


def test_boot_grace_suppresses_reboot(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    sd.mkdir()
    _first_failure(sd).write_text(str(1_000_000 - FAIL_WINDOW - 1))
    # Host came up 60s ago — inside the boot grace; never reboot yet.
    r = run(tmp_path, ping_ok="", now=1_000_000, uptime=60, state_dir=sd)
    assert r.returncode == 0, r.stderr
    # Grace also resets the window so it starts fresh once the stack settles.
    assert not _first_failure(sd).exists()


def test_recovery_clears_the_window(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    sd.mkdir()
    _first_failure(sd).write_text(str(1_000_000 - FAIL_WINDOW - 1))
    r = run(tmp_path, ping_ok="gw", now=1_000_000, state_dir=sd)
    assert r.returncode == 0, r.stderr
    assert not _first_failure(sd).exists()


def test_repair_reboots_and_writes_cooldown_marker(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    r = run(tmp_path, arg="repair", ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode != 0, "repair on a still-unreachable host must reboot"
    assert _marker(sd).read_text().strip() == "1000000"


def test_repair_recovered_between_test_and_repair_does_not_reboot(tmp_path: Path) -> None:
    # Network came back in the split second between the test and repair calls.
    sd = tmp_path / "state"
    r = run(tmp_path, arg="repair", ping_ok="gw", now=1_000_000, state_dir=sd)
    assert r.returncode == 0, r.stderr
    assert not _marker(sd).exists()


def test_cooldown_suppresses_a_second_reboot(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    sd.mkdir()
    _first_failure(sd).write_text(str(1_000_000 - FAIL_WINDOW - 1))
    # A reboot was requested 10 minutes ago; cooldown is 30 — suppress.
    _marker(sd).write_text(str(1_000_000 - 600))
    r_test = run(tmp_path, arg="test", ping_ok="", now=1_000_000, state_dir=sd)
    assert r_test.returncode == 0, "test must not signal during cooldown"
    r_repair = run(tmp_path, arg="repair", ping_ok="", now=1_000_000, state_dir=sd)
    assert r_repair.returncode == 0, "repair must not reboot during cooldown"


def test_cooldown_expiry_allows_reboot_again(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    sd.mkdir()
    _first_failure(sd).write_text(str(1_000_000 - FAIL_WINDOW - 1))
    _marker(sd).write_text(str(1_000_000 - REBOOT_COOLDOWN - 1))
    r = run(tmp_path, arg="repair", ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode != 0, "once the cooldown lapses a persistent outage may reboot again"
    assert _marker(sd).read_text().strip() == "1000000"


@pytest.mark.parametrize("bad_arg", ["", "bogus"])
def test_unknown_arg_is_treated_as_test(tmp_path: Path, bad_arg: str) -> None:
    # watchdog always passes "test"/"repair"; anything else must behave as a
    # (safe) test evaluation, never crash.
    r = run(tmp_path, arg=bad_arg, ping_ok="gw", now=1_000_000)
    assert r.returncode == 0, r.stderr


# --- Guard 5: never reboot under the package manager -------------------------------
# 2026-10-07, rocky: unpacking netplan mid-upgrade left the wired NIC unmanaged, the
# host went dark, and this script rebooted it in the middle of dpkg — twice (15:41,
# 16:47). Each later boot came up with the package DB half-upgraded, so the NIC stayed
# down and the loop repeated every cooldown. A reboot can never repair either state; it
# can only interrupt dpkg again. These pin the hold.

def _sustained(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir(exist_ok=True)
    _first_failure(sd).write_text(str(1_000_000 - FAIL_WINDOW - 1))
    return sd


def test_held_dpkg_lock_holds_a_sustained_outage(tmp_path: Path) -> None:
    sd = _sustained(tmp_path)
    _hold_dpkg_lock(tmp_path)
    r = run(tmp_path, ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode == 0, "must not signal a reboot while apt/dpkg holds its lock"
    assert "package manager" in r.stderr.lower()
    # The outage window restarts once dpkg is done, so the network gets a fresh 5 min.
    assert not _first_failure(sd).exists()


def test_held_dpkg_lock_vetoes_repair(tmp_path: Path) -> None:
    # The commit point too: repair must not reboot under a running upgrade.
    sd = tmp_path / "state"
    _hold_dpkg_lock(tmp_path)
    r = run(tmp_path, arg="repair", ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode == 0, r.stderr
    assert not _marker(sd).exists(), "a vetoed repair must not consume the cooldown"


@pytest.mark.parametrize("status", [
    "install ok unpacked", "install ok half-configured", "install reinstreq half-installed",
    "install ok triggers-pending", "install ok triggers-awaited",
])
def test_interrupted_dpkg_transaction_holds(tmp_path: Path, status: str) -> None:
    # No apt running (a fresh boot), but the DB is mid-upgrade: the reboot-loop case.
    sd = _sustained(tmp_path)
    (_dpkg_dir(tmp_path) / "status").write_text(
        f"Package: bash\nStatus: install ok installed\n\nPackage: netplan.io\nStatus: {status}\n\n")
    for arg in ("test", "repair"):
        r = run(tmp_path, arg=arg, ping_ok="", now=1_000_000, state_dir=sd)
        assert r.returncode == 0, f"{arg} with '{status}' must hold: {r.stderr}"
    assert not _marker(sd).exists()


def test_pending_dpkg_journal_holds(tmp_path: Path) -> None:
    # dpkg's own "you must run dpkg --configure -a" marker: entries in updates/.
    sd = _sustained(tmp_path)
    (_dpkg_dir(tmp_path) / "updates" / "0001").write_text("Package: netplan.io\n")
    r = run(tmp_path, ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode == 0, r.stderr


def test_unrelated_lock_and_settled_db_still_reboot(tmp_path: Path) -> None:
    # The guard must not neuter the watchdog: a lock on some OTHER inode and a settled
    # DB leave the normal sustained-outage reboot intact.
    sd = _sustained(tmp_path)
    inode = (_dpkg_dir(tmp_path) / "lock-frontend").stat().st_ino
    (tmp_path / "proc-locks").write_text(f"1: POSIX  ADVISORY  WRITE 1 08:02:{inode}9 0 EOF\n")
    r = run(tmp_path, ping_ok="", now=1_000_000, state_dir=sd)
    assert r.returncode != 0, "settled package DB + sustained outage must still reboot"


def test_reachable_host_never_consults_the_package_manager(tmp_path: Path) -> None:
    # Healthy path stays a single ping; the guard only runs once all targets are down.
    _hold_dpkg_lock(tmp_path)
    r = run(tmp_path, ping_ok="gw", now=1_000_000)
    assert r.returncode == 0 and "package manager" not in r.stderr.lower()
