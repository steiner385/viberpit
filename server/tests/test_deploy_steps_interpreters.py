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
