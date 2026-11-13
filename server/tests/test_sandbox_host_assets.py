"""#3367 fix round 1: the host-change assets in ``deploy/sandbox/``.

The scripts run for real against a shimmed ``sudo`` (it only records what it was asked
to do), so nothing is installed. The apt hook's command runs with its paths rewritten
into tmp_path.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "deploy" / "sandbox"
COPY = "/usr/libexec/viberpit/bwrap"


def _shims(tmp_path: Path, *, sudo_fails_on: str | None = None,
           stat_override: dict[str, str] | None = None) -> tuple[Path, Path]:
    """A bin dir with a recording ``sudo`` (optionally failing for one command) and,
    with *stat_override*, a ``stat`` that reports ``uid mode`` for chosen paths."""
    bindir = tmp_path / "shims"
    bindir.mkdir()
    log = tmp_path / "sudo.log"
    fail = f'[ "$1" = "{sudo_fails_on}" ] && exit 1\n' if sudo_fails_on else ""
    (bindir / "sudo").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n{fail}exit 0\n')
    (bindir / "sudo").chmod(0o755)
    if stat_override:
        cases = "".join(f'    "{p}") echo "{v}"; exit 0;;\n' for p, v in stat_override.items())
        (bindir / "stat").write_text(
            '#!/bin/sh\nfor last; do :; done\ncase "$last" in\n'
            f'{cases}esac\nexec /usr/bin/stat "$@"\n')
        (bindir / "stat").chmod(0o755)
    return bindir, log


def _run(script: str, bindir: Path, *args: str, sudo_user: str | None = None,
         managed: Path | None = None):
    """Run *script*; *managed* stands in for /etc/claude-code/managed-settings.json
    (default: a path that does not exist), so the host's real file never matters."""
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(bindir),
           "VIBERPIT_SANDBOX_MANAGED_SETTINGS": str(managed or bindir / "absent.json")}
    if sudo_user is not None:
        env["SUDO_USER"] = sudo_user
    return subprocess.run(["/bin/sh", str(ASSETS / script), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _me() -> tuple[str, str]:
    pw = pwd.getpwuid(os.getuid())
    import grp
    return pw.pw_name, grp.getgrgid(pw.pw_gid).gr_name


def _chain_is_root_owned() -> bool:
    d = os.path.dirname(COPY)
    while True:
        if os.path.lexists(d):
            st = os.lstat(d)
            if os.path.islink(d) or st.st_uid != 0 or st.st_mode & 0o022:
                return False
        if d == "/":
            return True
        d = os.path.dirname(d)


# ── one path everywhere, under a root-owned chain ────────────────────────────


def test_every_asset_names_the_same_copy_under_usr_libexec():
    """Fix round 1 [major]: /usr/local is tony:tony on dobby, so a copy under it could
    be swapped by tony. /usr/libexec is root:root all the way down."""
    profile = (ASSETS / "viberpit-sbx-bwrap").read_text()
    managed = json.loads((ASSETS / "managed-settings.json").read_text())
    hook = (ASSETS / "80viberpit-sbx-bwrap").read_text()
    install = (ASSETS / "install.sh").read_text()
    uninstall = (ASSETS / "uninstall.sh").read_text()
    assert re.search(rf"^profile viberpit-sbx-bwrap {re.escape(COPY)} ", profile, re.M)
    assert managed == {"sandbox": {"bwrapPath": COPY}}
    assert COPY in hook and COPY in uninstall
    assert "DEST_DIR=/usr/libexec/viberpit" in install
    for text in (profile, hook, install, uninstall, json.dumps(managed)):
        assert "/usr/local/libexec" not in text


def test_no_asset_hard_codes_the_viberpit_users_group():
    """Fix round 1 [minor]: the group comes from the named user (install) or from the
    existing copy (the hook), never a literal ``tony``."""
    for name in ("install.sh", "80viberpit-sbx-bwrap", "uninstall.sh"):
        text = (ASSETS / name).read_text()
        assert not re.search(r"-g\s+tony\b|root:tony", text), name
    assert "stat -c %G" in (ASSETS / "80viberpit-sbx-bwrap").read_text()


# ── install.sh ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("args,sudo_user,why", [
    ((), None, "name the ViberPit user"),
    (("no-such-user-3367",), None, "no such user: no-such-user-3367"),
    (("root",), None, "root is root"),
    ((), "no-such-user-3367", "no such user: no-such-user-3367"),
])
def test_install_validates_the_user_before_changing_anything(args, sudo_user, why, tmp_path):
    bindir, log = _shims(tmp_path)
    cp = _run("install.sh", bindir, *args, sudo_user=sudo_user)
    assert cp.returncode != 0 and why in cp.stderr
    assert _calls(log) == [], "nothing was run as root"


@pytest.mark.parametrize("uid_mode,why", [
    ("1000 755", "is not owned by root (uid 1000)"),
    ("0 775", "is writable by group or others (mode 775)"),
    ("0 1777", "is writable by group or others (mode 1777)"),
])
def test_install_refuses_a_target_chain_root_does_not_own_alone(uid_mode, why, tmp_path):
    """Fix round 1 [major]: any writable ancestor lets its owner swap the copy."""
    user, _group = _me()
    if os.getuid() == 0:
        pytest.skip("needs an unprivileged user to name")
    bindir, log = _shims(tmp_path, stat_override={"/usr/libexec": uid_mode})
    cp = _run("install.sh", bindir, user)
    assert cp.returncode != 0
    assert "/usr/libexec " in cp.stderr and why in cp.stderr
    assert _calls(log) == []


def test_install_runs_the_plan_steps_with_the_users_group(tmp_path):
    if os.getuid() == 0:
        pytest.skip("needs an unprivileged user to name")
    if not _chain_is_root_owned():
        pytest.skip("this host's /usr/libexec chain is not root-owned")
    user, group = _me()
    bindir, log = _shims(tmp_path)
    cp = _run("install.sh", bindir, sudo_user=user)          # $SUDO_USER by default
    assert cp.returncode == 0, cp.stderr
    calls = _calls(log)
    copy = f"install -o root -g {group} -m 0750 /usr/bin/bwrap {COPY}"
    load = "apparmor_parser -r -W /etc/apparmor.d/viberpit-sbx-bwrap"
    assert copy in calls and load in calls
    assert calls.index(load) < calls.index(copy), "the profile is loaded before the copy"
    assert calls[0] == "install -d -o root -g root -m 0755 /usr/libexec/viberpit"
    absent = bindir / "absent.json"
    assert f"install -o root -g root -m 0644 managed-settings.json {absent}" in calls


def _settings(tmp_path: Path, body: str | None) -> Path:
    path = tmp_path / "etc" / "managed-settings.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(body if body is not None else (ASSETS / "managed-settings.json").read_text())
    return path


def test_install_refuses_to_overwrite_someone_elses_managed_settings(tmp_path):
    """Re-review N2: /etc/claude-code/managed-settings.json is host-wide CLI policy;
    another admin's file is merged by hand, never replaced."""
    if os.getuid() == 0:
        pytest.skip("needs an unprivileged user to name")
    user, _group = _me()
    theirs = '{"permissions": {"deny": ["WebSearch"]}}\n'
    managed = _settings(tmp_path, theirs)
    bindir, log = _shims(tmp_path)
    cp = _run("install.sh", bindir, user, managed=managed)
    assert cp.returncode != 0
    assert "differs from deploy/sandbox/managed-settings.json" in cp.stderr
    assert "merge" in cp.stderr and "bwrapPath" in cp.stderr and COPY in cp.stderr
    assert _calls(log) == [], "refused before anything changed"
    assert managed.read_text() == theirs


def test_install_leaves_identical_managed_settings_alone(tmp_path):
    if os.getuid() == 0:
        pytest.skip("needs an unprivileged user to name")
    if not _chain_is_root_owned():
        pytest.skip("this host's /usr/libexec chain is not root-owned")
    user, _group = _me()
    managed = _settings(tmp_path, None)
    bindir, log = _shims(tmp_path)
    cp = _run("install.sh", bindir, user, managed=managed)
    assert cp.returncode == 0, cp.stderr
    assert not [c for c in _calls(log) if str(managed) in c], "not rewritten"
    assert "left as it is" in cp.stdout


# ── uninstall.sh ─────────────────────────────────────────────────────────────


def test_uninstall_removes_the_copy_and_the_hook_even_if_the_unload_fails(tmp_path):
    """Fix round 1 [minor]: an already-unloaded profile must not strand the copy."""
    bindir, log = _shims(tmp_path, sudo_fails_on="apparmor_parser")
    managed = _settings(tmp_path, None)                   # ours, byte for byte
    cp = _run("uninstall.sh", bindir, managed=managed)
    assert cp.returncode == 0, cp.stderr
    calls = _calls(log)
    assert calls[0] == f"rm -f {managed}"
    assert f"rm -f {COPY} /etc/apt/apt.conf.d/80viberpit-sbx-bwrap" in calls
    assert "rm -f /etc/apparmor.d/viberpit-sbx-bwrap" in calls


@pytest.mark.parametrize("body", ['{"sandbox": {"bwrapPath": "/usr/libexec/viberpit/bwrap"}, '
                                  '"permissions": {"deny": ["WebSearch"]}}\n', "link"])
def test_uninstall_leaves_managed_settings_that_are_not_ours(body, tmp_path):
    """Re-review N2: only a byte-identical copy of our file is deleted; a merged file
    (or a symlink) is left in place with a note."""
    bindir, log = _shims(tmp_path)
    if body == "link":
        managed = tmp_path / "etc" / "managed-settings.json"
        managed.parent.mkdir()
        managed.symlink_to(ASSETS / "managed-settings.json")
    else:
        managed = _settings(tmp_path, body)
    cp = _run("uninstall.sh", bindir, managed=managed)
    assert cp.returncode == 0, cp.stderr
    assert "not ViberPit's file; left in place" in cp.stderr
    calls = _calls(log)
    assert not [c for c in calls if str(managed) in c]
    assert f"rm -f {COPY} /etc/apt/apt.conf.d/80viberpit-sbx-bwrap" in calls


def test_uninstall_without_managed_settings_skips_that_step(tmp_path):
    bindir, log = _shims(tmp_path)
    cp = _run("uninstall.sh", bindir)
    assert cp.returncode == 0 and "absent.json" not in "".join(_calls(log))


def test_uninstall_keeps_going_past_a_failed_step_and_says_so(tmp_path):
    bindir, log = _shims(tmp_path, sudo_fails_on="rm")
    cp = _run("uninstall.sh", bindir)
    assert cp.returncode == 1 and "failed (continuing)" in cp.stderr
    assert any(c.startswith("apparmor_parser -R") for c in _calls(log))


# ── the apt hook ─────────────────────────────────────────────────────────────


def _hook_command(tmp_path: Path, copy_dir: Path, stock: Path) -> str:
    text = (ASSETS / "80viberpit-sbx-bwrap").read_text()
    m = re.fullmatch(r'DPkg::Post-Invoke \{ "(.*)"; \};\n', text, re.S)
    assert m, "one Post-Invoke string"
    return (m.group(1).replace("/usr/libexec/viberpit", str(copy_dir))
            .replace("/usr/bin/bwrap", str(stock)))


@pytest.mark.parametrize("state", ["no-dir", "no-copy", "install-fails", "current"])
def test_the_apt_hook_never_fails_apt(state, tmp_path):
    """Fix round 1 [minor]: a Post-Invoke that exits non-zero fails the apt run. The
    hook is a no-op without the copy and swallows its own failure; a missed update is
    the preflight's ``bwrap_copy`` stale check's to catch."""
    stock = tmp_path / "bwrap"
    stock.write_bytes(b"bwrap 0.11.2")
    stock.chmod(0o755)
    copy_dir = tmp_path / "viberpit"
    if state != "no-dir":
        copy_dir.mkdir()
    if state in ("install-fails", "current"):
        copy = copy_dir / "bwrap"
        copy.write_bytes(b"bwrap 0.11.2" if state == "current" else b"bwrap 0.11.1")
        copy.chmod(0o750)
    cmd = _hook_command(tmp_path, copy_dir, stock)
    cp = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=30,
                        env={"PATH": "/usr/bin:/bin"})
    assert cp.returncode == 0, (state, cp.stderr)
    if state == "no-dir":
        assert not copy_dir.exists(), "never creates the dir"
    if state == "no-copy":
        assert list(copy_dir.iterdir()) == [], "never creates a copy that was removed"
