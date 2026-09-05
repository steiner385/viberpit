"""Preflight: the units this deploy restarts must have a runnable ExecStart binary.

2026-09-04: `ccswitch-daemon.service` runs
`/home/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m server.ipc`, and that
`.venv` is a symlink into a *different* checkout. Renaming that checkout left the symlink
dangling. The running daemon kept working — a live process holds open inodes, not paths —
so nothing looked wrong, and the breakage was invisible until something tried to exec the
interpreter again. A deploy restarts the daemon, so the next deploy would have taken every
live session down with no way back up.

This is the same shape as the other preflight checks (CI verdict, working tree, target
ancestry): a precondition nothing verified until it was too late to matter.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from server.deploy import steps


def _fake_run(paths: dict[str, str | None]):
    """systemctl stub: maps unit -> ExecStart line (or None to simulate failure)."""
    def run(argv, **kwargs):
        unit = argv[-1]
        line = paths.get(unit, "")
        if line is None:
            raise subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(returncode=0, stdout=line, stderr="")
    return run


# systemd's real shape for `systemctl show -p ExecStart <unit>`.
def _execstart(path: str) -> str:
    return (f"{{ path={path} ; argv[]={path} -m server.ipc ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")


def test_no_problems_when_every_interpreter_exists(tmp_path):
    exe = tmp_path / "python"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    run = _fake_run({"ccswitch.service": _execstart(str(exe))})
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_dangling_interpreter_is_reported(tmp_path):
    """The exact production failure: ExecStart points at a path that no longer exists."""
    missing = tmp_path / "gone" / "python"
    run = _fake_run({"ccswitch-daemon.service": _execstart(str(missing))})
    problems = steps.check_unit_interpreters(("ccswitch-daemon.service",), run=run)
    assert len(problems) == 1
    assert "ccswitch-daemon.service" in problems[0]
    assert str(missing) in problems[0], "the problem must name the path so it is actionable"


def test_dangling_symlink_is_reported(tmp_path):
    """A symlink whose target moved — how the real breakage actually presented.

    `os.path.exists` follows symlinks, so a dangling link reads as missing. Pinned
    because a naive `lexists` would call this healthy and miss the whole bug.
    """
    link = tmp_path / "python"
    link.symlink_to(tmp_path / "moved-away" / "python")
    run = _fake_run({"ccswitch.service": _execstart(str(link))})
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) != []


def test_non_executable_interpreter_is_reported(tmp_path):
    exe = tmp_path / "python"
    exe.write_text("")
    exe.chmod(0o644)
    run = _fake_run({"ccswitch.service": _execstart(str(exe))})
    problems = steps.check_unit_interpreters(("ccswitch.service",), run=run)
    assert len(problems) == 1 and "not executable" in problems[0]


def test_unresolvable_unit_is_skipped_not_failed():
    """A check that blocks on "cannot determine" is a wedge, not a safeguard.

    If systemctl fails (unit absent, no systemd, a container), we have NO evidence of
    breakage — and a preflight that refuses on missing evidence is exactly the failure
    mode that left the deploy gate permanently stuck on a required check that could never
    report. Fail only on positive proof that a path is broken.
    """
    run = _fake_run({"ccswitch.service": None})           # systemctl raised
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_empty_execstart_is_skipped_not_failed():
    run = _fake_run({"ccswitch.service": ""})             # no ExecStart property
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_unparseable_execstart_is_skipped_not_failed():
    run = _fake_run({"ccswitch.service": "{ argv[]=python -m server.ipc ; }"})  # no path=
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_interpreter_escaping_its_own_checkout_is_reported(tmp_path):
    """Issue #1171: the exact production coupling — ExecStart's ``.venv`` is a symlink
    into a *different* developer checkout, not a broken link but a live one. The unit
    still runs, but renaming or rebuilding that other checkout breaks it, and nothing
    short of following the symlink to see where it actually leads catches that.
    """
    checkout_a = tmp_path / "cc-account-switcher-deploy"
    checkout_b = tmp_path / "ccswitch"
    real_exe = checkout_b / "actual-venv" / "bin" / "python"
    real_exe.parent.mkdir(parents=True)
    real_exe.write_text("#!/bin/sh\n")
    real_exe.chmod(0o755)
    venv_link = checkout_a / ".venv"
    venv_link.parent.mkdir(parents=True)
    venv_link.symlink_to(checkout_b / "actual-venv")
    exe_path = str(checkout_a / ".venv" / "bin" / "python")

    run = _fake_run({"ccswitch-daemon.service": _execstart(exe_path)})
    problems = steps.check_unit_interpreters(("ccswitch-daemon.service",), run=run)

    assert len(problems) == 1
    assert exe_path in problems[0] and str(checkout_a) in problems[0]
    assert str(real_exe.parent.parent) in problems[0], (
        "the resolved venv target must be named so it is actionable")


def test_interpreter_within_its_own_checkout_via_symlink_is_not_reported(tmp_path):
    """A symlink is fine as long as it stays inside the checkout ExecStart names."""
    checkout = tmp_path / "ccswitch"
    real_exe = checkout / "venv-actual" / "bin" / "python"
    real_exe.parent.mkdir(parents=True)
    real_exe.write_text("#!/bin/sh\n")
    real_exe.chmod(0o755)
    (checkout / ".venv").symlink_to(checkout / "venv-actual")
    exe_path = str(checkout / ".venv" / "bin" / "python")

    run = _fake_run({"ccswitch.service": _execstart(exe_path)})
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_standard_venv_system_interpreter_symlink_is_not_reported(tmp_path):
    """A venv-owned launcher may normally resolve to the host Python executable.

    The isolation boundary is the ``.venv`` directory, not ``bin/python``: standard
    virtualenvs leave the directory inside the checkout while symlinking the launcher
    to a system interpreter such as ``/usr/bin/python3.12``.
    """
    checkout = tmp_path / "ccswitch"
    bin_dir = checkout / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    system_python = tmp_path / "usr" / "bin" / "python3.12"
    system_python.parent.mkdir(parents=True)
    system_python.write_text("#!/bin/sh\n")
    system_python.chmod(0o755)
    launcher = bin_dir / "python"
    launcher.symlink_to(system_python)

    run = _fake_run({"ccswitch.service": _execstart(str(launcher))})
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_path_without_venv_marker_skips_the_isolation_check(tmp_path):
    """Not every ExecStart follows the ``<checkout>/.venv/bin/python`` convention (e.g.
    a future service-owned venv path). Without that marker there is no notion of "which
    checkout this belongs to", so the isolation check has no positive proof to act on.
    """
    exe = tmp_path / "opt-style" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    run = _fake_run({"ccswitch.service": _execstart(str(exe))})
    assert steps.check_unit_interpreters(("ccswitch.service",), run=run) == []


def test_every_unit_is_checked_and_all_problems_reported(tmp_path):
    """One bad unit must not mask another — the operator should see every path to fix."""
    good = tmp_path / "python"
    good.write_text("")
    good.chmod(0o755)
    bad1, bad2 = tmp_path / "a" / "python", tmp_path / "b" / "python"
    run = _fake_run({
        "ccswitch.service": _execstart(str(good)),
        "ccswitch-daemon.service": _execstart(str(bad1)),
        "ccswitch-deploy-controller.service": _execstart(str(bad2)),
    })
    problems = steps.check_unit_interpreters(
        ("ccswitch.service", "ccswitch-daemon.service",
         "ccswitch-deploy-controller.service"), run=run)
    assert len(problems) == 2
    assert any(str(bad1) in p for p in problems) and any(str(bad2) in p for p in problems)


def test_default_units_cover_what_a_deploy_restarts():
    """The edge, the local daemon and the controller are all restarted by a deploy."""
    assert set(steps.RESTARTABLE_UNITS) == {
        "ccswitch.service", "ccswitch-daemon.service", "ccswitch-deploy-controller.service"}


@pytest.mark.asyncio
async def test_preflight_refuses_before_any_change_when_interpreter_is_broken():
    """Wired into preflight: refuse early, restart nothing, roll nothing back."""
    from server.tests.test_deploy_controller import FakeGit, make_steps, make_runners, make_controller
    from server.deploy.types import PhaseState

    g = FakeGit("a", "b", [])
    s, calls = make_steps()
    s.check_unit_interpreters = lambda units=None, run=None: [
        "ccswitch-daemon.service: ExecStart binary /nope/python does not exist"]
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()

    assert st.error and "preflight" in st.error
    assert "ccswitch-daemon.service" in st.error, "the failing unit must be named"
    assert st.phases["preflight"] == PhaseState.FAILED.value
    assert calls.restarted == [] and calls.rolled_back is False
    assert rc.deploy_kwargs is None, "nothing fanned out to the fleet"
