# server/tests/test_mounts_unit.py
"""Regression coverage for the 2026-08-26 rocky reboot outage.

Sibling of #765 / ``test_daemon_ensure_unit.py``. Same failure shape, one layer down.

At 09:20 rocky came up after a watchdog reboot. ``viberpit-mounts.service`` ran while
tailscaled was still settling, and the FIRST sshfs attempt for
``/home/tony/.claude-accounts`` died with ``read: Connection reset by peer``. The unit
exited 1, which cancelled ``viberpit-daemon.service``'s start job outright
(``Requires=viberpit-mounts.service``) — and a cancelled start job never engages
``Restart=always``. The mount unit's own ``Restart=on-failure`` recovered the mounts ten
seconds later, but nothing re-tried the daemon; the fleet stayed down until
``viberpit-daemon-ensure.timer`` fired ~60s later.

The ensure timer is the safety net and it worked. THIS file covers the trigger: the mount
script must survive a not-yet-ready tailnet on its first invocation, so the dependency
never fails and the daemon is never cancelled in the first place.

The tests execute the real script against stubbed ``sshfs``/``mountpoint`` rather than
grepping it for keywords — a retry loop that does not actually retry passes a grep.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "bin-viberpit-mounts.sh"
UNIT = ROOT / "deploy" / "viberpit-mounts.service"
DROP_IN = ROOT / "deploy" / "viberpit-daemon.service.d" / "require-mounts.conf"


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
# Record the -o value this path was mounted with so the cache-profile tests (#1005)
# can assert on it instead of grepping the script.
opts=""
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && opts="$a"
  prev="$a"
done
echo "$opts" > "{state}/opts_$key"
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


def _run(tmp_path: Path, fail_times: int, attempts: int, paths: list[str], **extra_env):
    bindir = _make_shims(tmp_path, fail_times)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCSWITCH_MOUNT_PATHS"] = " ".join(paths)
    env["CCSWITCH_MOUNT_ATTEMPTS"] = str(attempts)
    env["CCSWITCH_MOUNT_RETRY_DELAY"] = "0"
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def _opts_for(tmp_path: Path, path: str) -> str:
    """The `-o` value the stubbed sshfs was invoked with for *path*."""
    key = path.replace("/", "_")
    return (tmp_path / "state" / f"opts_{key}").read_text().strip()


def test_mount_script_is_owned_by_the_repo():
    """The rocky-only copy at /home/tony/bin-viberpit-mounts.sh was untracked drift."""
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
    assert "Requires=viberpit-mounts.service" in drop_in
    assert "After=viberpit-mounts.service" in drop_in


# --- #1005: cache-profile split (coherent account store vs read-mostly archives) ------
#
# The 600s attr/entry cache (#779, tuned by #826/#851) is only sound for a mount dobby
# does NOT rewrite underneath us. The account store is rewritten continuously by dobby's
# live sessions (atomic temp-file + rename), so a cached inode/size gives rocky a
# truncated read -> Claude Code calls it corruption -> it writes a DEFAULT config back
# through the mount. ~1,100 shared-config resets/day from 2026-09-01 (#1005).
#
# These execute the real script and read the `-o` the stub sshfs actually received: a
# profile table that never reaches sshfs would pass a grep.


def test_account_store_is_mounted_without_attribute_caching(tmp_path):
    """THE regression: a file another host rewrites must not be attribute-cached."""
    store = tmp_path / "claude-accounts"
    res = _run(tmp_path, fail_times=0, attempts=1, paths=[str(store)],
               CCSWITCH_MOUNT_COHERENT_PATHS=str(store))
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    opts = _opts_for(tmp_path, str(store))
    assert "attr_timeout=0" in opts, opts
    assert "entry_timeout=0" in opts, opts
    assert "dcache_timeout=0" in opts, opts
    assert "dir_cache=yes" not in opts, opts
    # The connection-resilience half of the profile is unchanged — dropping `reconnect`
    # would trade this bug for the #765 dead-mount outage.
    assert "reconnect" in opts, opts


def test_read_mostly_mounts_keep_the_long_cache(tmp_path):
    """#826/#851's latency win must survive: only the account store pays for coherence."""
    store, archive = tmp_path / "claude-accounts", tmp_path / "hdd-storage"
    res = _run(tmp_path, fail_times=0, attempts=1, paths=[str(store), str(archive)],
               CCSWITCH_MOUNT_COHERENT_PATHS=str(store))
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    archive_opts = _opts_for(tmp_path, str(archive))
    assert "attr_timeout=600" in archive_opts, archive_opts
    assert "entry_timeout=600" in archive_opts, archive_opts
    assert "dir_cache=yes" in archive_opts, archive_opts


def test_the_shipped_default_puts_the_account_store_in_the_coherent_profile(tmp_path):
    """Defaults matter — rocky runs this with no CCSWITCH_MOUNT_* overrides at all.

    Drives the script's real DEFAULT_COHERENT_PATHS by naming the production account
    store path in CCSWITCH_MOUNT_PATHS while leaving the coherent list unset.
    """
    res = _run(tmp_path, fail_times=0, attempts=1,
               paths=["/home/tony/.claude-accounts", "/mnt/hdd-storage"])
    # mkdir/mount of the real paths is stubbed, so this exercises the profile table only.
    assert "attr_timeout=0" in _opts_for(tmp_path, "/home/tony/.claude-accounts")
    assert "attr_timeout=600" in _opts_for(tmp_path, "/mnt/hdd-storage")


def test_live_mount_is_still_left_alone_unless_a_remount_is_forced(tmp_path):
    """A cache-profile change must not silently unmount a store sessions are reading.

    Re-mounting drops every in-flight read on the path, so it stays operator-driven.
    """
    store = tmp_path / "claude-accounts"
    store.mkdir()
    bindir = _make_shims(tmp_path, fail_times=0)
    marker = tmp_path / "state" / f"mounted_{str(store).replace('/', '_')}"
    marker.touch()
    base = dict(os.environ)
    base["PATH"] = f"{bindir}:{base['PATH']}"
    base["CCSWITCH_MOUNT_PATHS"] = str(store)
    base["CCSWITCH_MOUNT_ATTEMPTS"] = "1"
    base["CCSWITCH_MOUNT_RETRY_DELAY"] = "0"

    res = subprocess.run(["bash", str(SCRIPT)], env=base, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 0
    assert not (tmp_path / "state" / "calls").exists(), "must not re-sshfs a healthy mount"

    forced = dict(base, CCSWITCH_MOUNT_FORCE_REMOUNT="1")
    res = subprocess.run(["bash", str(SCRIPT)], env=forced, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    assert (tmp_path / "state" / "calls").exists(), "forced remount must re-run sshfs"


def test_canonical_units_keep_legacy_aliases_without_duplicate_templates():
    units = list((ROOT / "deploy").glob("viberpit*.service"))
    units += list((ROOT / "deploy").glob("viberpit*.timer"))
    assert len(units) == 12
    for canonical in units:
        legacy = canonical.with_name(canonical.name.replace("viberpit", "ccswitch", 1))
        assert legacy.is_symlink(), legacy
        assert legacy.resolve() == canonical
        # Remote template is installed as viberpit-daemon.service on the runner.
        alias = legacy.name.replace("-remote.service", ".service")
        assert f"Alias={alias}" in canonical.read_text(), canonical


def test_authority_identity_stays_private_across_unit_rename():
    authority = (ROOT / "deploy/viberpit-codex-auth.service").read_text()
    assert "User=ccswitch-auth" in authority
    assert "Group=ccswitch-auth" in authority
    assert "StateDirectory=ccswitch-codex-auth" in authority
    assert "StateDirectoryMode=0700" in authority
    assert "EnvironmentFile=/etc/viberpit/codex-auth.env" in authority


def test_daemon_units_share_canonical_runtime_and_state_roots():
    for name in ("viberpit-daemon.service", "viberpit-daemon-remote.service"):
        unit = (ROOT / "deploy" / name).read_text()
        assert "RuntimeDirectory=viberpit\n" in unit
        assert "StateDirectory=viberpit\n" in unit
        assert "EnvironmentFile=/etc/viberpit/viberpit.env" in unit
        assert "Environment=XDG_STATE_HOME=%S/viberpit/state" in unit


@pytest.mark.parametrize("remote", [False, True])
def test_canonical_templates_pass_real_systemd_parser(tmp_path, remote):
    """Validate units/drop-ins with the real parser, without touching the host manager.

    Fixture executables and dependency targets stand in for host installations;
    this checks systemd syntax/dependency loading, not actual service startup.
    """
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is not installed")
    unit_dir = tmp_path / "etc/systemd/system"
    unit_dir.mkdir(parents=True)
    templates = list((ROOT / "deploy").glob("viberpit*.service"))
    templates += list((ROOT / "deploy").glob("viberpit*.timer"))
    for template in templates:
        if template.name == "viberpit-daemon-remote.service":
            continue
        source = template
        if remote and template.name == "viberpit-daemon.service":
            source = template.with_name("viberpit-daemon-remote.service")
        content = source.read_text()
        (unit_dir / template.name).write_text(content)
        for alias in re.findall(r"^Alias=(.+)$", content, re.MULTILINE):
            (unit_dir / alias).symlink_to(template.name)
        for command in re.findall(r"^Exec\w+=-?([^\s\\]+)", content, re.MULTILINE):
            executable = tmp_path / command.lstrip("/") if command.startswith("/") else (
                tmp_path / "usr/bin" / command
            )
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        for env_file in re.findall(r"^EnvironmentFile=-?(.+)$", content, re.MULTILINE):
            fixture_env = tmp_path / env_file.lstrip("/")
            fixture_env.parent.mkdir(parents=True, exist_ok=True)
            fixture_env.touch()
    dropin_name = "viberpit-daemon.service.d" if remote else "viberpit.service.d"
    shutil.copytree(ROOT / "deploy" / dropin_name, unit_dir / dropin_name)
    for target in ("sysinit", "basic", "shutdown", "multi-user", "timers", "network-online"):
        (unit_dir / f"{target}.target").write_text("[Unit]\nDescription=Fixture dependency\n")
    (unit_dir / "tailscaled.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    true = tmp_path / "usr/bin/true"
    true.parent.mkdir(parents=True, exist_ok=True)
    true.write_text("#!/bin/sh\nexit 0\n")
    true.chmod(0o755)
    result = subprocess.run(
        [analyzer, f"--root={tmp_path}", "verify",
         *map(str, unit_dir.glob("viberpit*.service")),
         *map(str, unit_dir.glob("viberpit*.timer"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
