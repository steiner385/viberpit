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
