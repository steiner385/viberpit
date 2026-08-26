# server/tests/test_mounts_unit.py
"""Regression coverage for the 2026-08-26 rocky reboot outage.

Sibling of #765 / ``test_daemon_ensure_unit.py``. Same failure shape, one layer down.

At 09:20 rocky came up after a watchdog reboot. ``ccswitch-mounts.service`` ran while
tailscaled was still settling, and the FIRST sshfs attempt for
``/home/tony/.claude-accounts`` died with ``read: Connection reset by peer``. The unit
exited 1, which cancelled ``ccswitch-daemon.service``'s start job outright
(``Requires=ccswitch-mounts.service``) — and a cancelled start job never engages
``Restart=always``. The mount unit's own ``Restart=on-failure`` recovered the mounts ten
seconds later, but nothing re-tried the daemon; the fleet stayed down until
``ccswitch-daemon-ensure.timer`` fired ~60s later.

The ensure timer is the safety net and it worked. THIS file covers the trigger: the mount
script must survive a not-yet-ready tailnet on its first invocation, so the dependency
never fails and the daemon is never cancelled in the first place.

The tests execute the real script against stubbed ``sshfs``/``mountpoint`` rather than
grepping it for keywords — a retry loop that does not actually retry passes a grep.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "bin-ccswitch-mounts.sh"
UNIT = ROOT / "deploy" / "ccswitch-mounts.service"
DROP_IN = ROOT / "deploy" / "ccswitch-daemon.service.d" / "require-mounts.conf"


def _make_shims(tmp_path: Path, fail_times: int) -> Path:
    """PATH shims: sshfs fails `fail_times` times, then succeeds.

    A "mounted" path is recorded as a marker file so `mountpoint` agrees with reality.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    (bindir / "sshfs").write_text(
        f"""#!/bin/bash
target="${{@: -1}}"
key=$(printf '%s' "$target" | tr / _)
n=$(cat "{state}/calls" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "{state}/calls"
if [ "$n" -le {fail_times} ]; then
  echo "read: Connection reset by peer" >&2
  exit 1
fi
touch "{state}/mounted_$key"
exit 0
"""
    )
    (bindir / "mountpoint").write_text(
        f"""#!/bin/bash
p="${{@: -1}}"
key=$(printf '%s' "$p" | tr / _)
[ -f "{state}/mounted_$key" ] && exit 0 || exit 1
"""
    )
    # `timeout N cmd...` -> just run cmd
    (bindir / "timeout").write_text('#!/bin/bash\nshift\nexec "$@"\n')
    (bindir / "fusermount").write_text("#!/bin/bash\nexit 0\n")
    (bindir / "fusermount3").write_text("#!/bin/bash\nexit 0\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


def _run(tmp_path: Path, fail_times: int, attempts: int, paths: list[str]):
    bindir = _make_shims(tmp_path, fail_times)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCSWITCH_MOUNT_PATHS"] = " ".join(paths)
    env["CCSWITCH_MOUNT_ATTEMPTS"] = str(attempts)
    env["CCSWITCH_MOUNT_RETRY_DELAY"] = "0"
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_mount_script_is_owned_by_the_repo():
    """The rocky-only copy at /home/tony/bin-ccswitch-mounts.sh was untracked drift."""
    assert SCRIPT.exists(), f"{SCRIPT} missing — deploy/ must own the mount script"


def test_transient_sshfs_failure_at_boot_still_mounts(tmp_path):
    """THE regression. Two failed attempts (tailnet settling) must not fail the unit.

    Before the fix the script made exactly one sshfs attempt per path, so this exact
    sequence exited 1, cancelled the daemon's start job, and took the fleet down.
    """
    p = tmp_path / "accounts"
    res = _run(tmp_path, fail_times=2, attempts=5, paths=[str(p)])
    assert res.returncode == 0, f"expected success after retries\n{res.stdout}\n{res.stderr}"


def test_retries_are_bounded_and_the_unit_still_fails_when_dobby_is_really_down(tmp_path):
    """Retry must not mask a genuine outage, nor hang past the unit's start timeout."""
    p = tmp_path / "accounts"
    res = _run(tmp_path, fail_times=99, attempts=3, paths=[str(p)])
    assert res.returncode != 0, "a permanently unreachable host must still fail the unit"
    calls = int((tmp_path / "state" / "calls").read_text().strip())
    assert calls == 3, f"expected exactly 3 bounded attempts, got {calls}"


def test_already_healthy_mount_is_left_alone(tmp_path):
    """Idempotency: re-running must not tear down a live mount (sessions are using it)."""
    p = tmp_path / "accounts"
    p.mkdir()  # a live mount is a real, readable directory — the script probes it with ls
    bindir = _make_shims(tmp_path, fail_times=0)
    (tmp_path / "state" / f"mounted_{str(p).replace('/', '_')}").touch()
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCSWITCH_MOUNT_PATHS"] = str(p)
    env["CCSWITCH_MOUNT_ATTEMPTS"] = "5"
    env["CCSWITCH_MOUNT_RETRY_DELAY"] = "0"
    res = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0
    assert not (tmp_path / "state" / "calls").exists(), "must not re-sshfs a healthy mount"


def test_every_path_is_attempted_even_when_an_earlier_one_fails(tmp_path):
    """One dead path must not silently skip the rest — the daemon needs all of them."""
    a, b = tmp_path / "a", tmp_path / "b"
    res = _run(tmp_path, fail_times=99, attempts=2, paths=[str(a), str(b)])
    assert res.returncode != 0
    calls = int((tmp_path / "state" / "calls").read_text().strip())
    assert calls == 4, f"expected 2 paths x 2 attempts = 4, got {calls}"


def test_mount_unit_and_daemon_drop_in_are_owned_by_the_repo():
    assert UNIT.exists(), f"{UNIT} missing"
    assert DROP_IN.exists(), f"{DROP_IN} missing"
    unit = UNIT.read_text()
    # The outer safety net stays: retry inside the script is belt, this is braces.
    assert "Restart=on-failure" in unit
    # Bounded retry must fit inside the unit's start timeout, or systemd kills it mid-retry.
    assert "TimeoutStartSec=" in unit


def test_daemon_drop_in_still_requires_the_mounts():
    """Do NOT 'fix' the race by loosening Requires= to Wants=.

    Starting the daemon into a dead sshfs mount is what caused #765
    (OSError: [Errno 5] Input/output error on /home/tony/.claude-accounts).
    """
    drop_in = DROP_IN.read_text()
    assert "Requires=ccswitch-mounts.service" in drop_in
    assert "After=ccswitch-mounts.service" in drop_in
