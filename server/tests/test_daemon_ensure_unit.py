# server/tests/test_daemon_ensure_unit.py
"""Regression coverage for #765: the daemon stayed dead for 45 minutes after a
cancelled start job.

``ccswitch-daemon.service`` has ``Requires=ccswitch-mounts.service``. When that mount
unit is not ready at boot, systemd CANCELS the daemon's start job outright and the unit
sits inactive forever — ``Restart=always`` never engages, because it only restarts a
process that actually ran and exited. On 2026-08-25 the sshfs mount recovered on its own
minutes after boot, but nothing ever retried the daemon.

``ccswitch-daemon-ensure.timer`` closes that gap ON the daemon host, so recovery needs
neither the network nor SSH (both were unavailable during the incident).

The load-bearing assertion here is the MOUNT GUARD. Starting the daemon into a dead
sshfs mount is what killed it in the first place (``OSError: [Errno 5] Input/output
error: '/home/tony/.claude-accounts'``), so a down daemon plus down mounts must be left
alone for the mount unit to fix. An ensure-unit without that guard would turn a
self-limiting outage into a crash loop.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "deploy" / "ccswitch-daemon-ensure.service"
TIMER = ROOT / "deploy" / "ccswitch-daemon-ensure.timer"


def test_ensure_unit_exists_and_is_oneshot():
    unit = UNIT.read_text()
    assert "Type=oneshot" in unit


def test_ensure_unit_no_ops_when_daemon_already_active():
    """It must not restart a healthy daemon — that is Restart='s job, not this one."""
    unit = UNIT.read_text()
    assert "is-active --quiet ccswitch-daemon.service" in unit
    assert "exit 0" in unit


def test_ensure_unit_refuses_to_start_the_daemon_without_healthy_mounts():
    """THE safety property. A down daemon plus down mounts is not this unit's to fix.

    Without this guard the unit would start the daemon straight into the broken sshfs
    mount that killed it, once a minute, forever.
    """
    unit = UNIT.read_text()
    assert "is-active --quiet ccswitch-mounts.service" in unit, (
        "the ensure unit must gate on the mount unit, not merely on the daemon being down"
    )
    exec_start = unit.split("ExecStart=", 1)[1]
    guard = exec_start.index("ccswitch-mounts.service")
    start = exec_start.index("systemctl start ccswitch-daemon.service")
    assert guard < start, "the mount check must precede the start, or it guards nothing"


def test_ensure_unit_runs_as_root():
    """`systemctl start` on a system unit needs root; a User= line would break it."""
    unit = UNIT.read_text()
    assert not any(line.startswith("User=") for line in unit.splitlines()), (
        "ExecStart calls systemctl start on a system unit, which requires root"
    )


def test_timer_fires_soon_after_boot_and_then_periodically():
    """The race this fixes happens seconds after boot, so OnBootSec must be short."""
    timer = TIMER.read_text()
    boot = next(line for line in timer.splitlines() if line.startswith("OnBootSec="))
    assert int(boot.split("=", 1)[1]) <= 60, "a long OnBootSec is outage time"
    assert "OnUnitActiveSec=" in timer
    assert "Unit=ccswitch-daemon-ensure.service" in timer
    assert "WantedBy=timers.target" in timer


def test_health_check_no_longer_claims_restart_always_covers_this():
    """The health watchdog declined to act on a premise this incident disproved.

    It logged "not our job (systemd Restart=always owns this)" every 60s for the whole
    outage. Leaving that reasoning documented as true is how the gap reappears.
    """
    script = (ROOT / "scripts" / "ccswitch-health-check.sh").read_text()
    assert "Restart=always already owns recovery" not in script, (
        "the false premise from #765 must not be restated as fact"
    )
    assert "ccswitch-daemon-ensure.timer" in script, (
        "the script should name what actually covers the cancelled-start-job case"
    )
