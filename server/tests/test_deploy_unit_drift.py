"""#2702: a merged daemon-unit safety fix that was never installed must not stay silent.

#2690 set ``OOMPolicy=continue`` in ``deploy/viberpit-daemon.service`` (#2673), merged,
and deployed — but a deploy fast-forwards unit FILES and never installs them, so dobby's
live unit kept ``OOMPolicy=stop`` and the next host-wide OOM stopped every session again.
Every deploy now compares the safety directives the repo declares against the installed
unit on each runner and reports the drift as a deploy warning.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from server.deploy import runners as R
from server.deploy import steps

UNIT = "viberpit-daemon.service"


def _systemd(installed: str, calls: list | None = None, *, rc: int = 0):
    """``systemctl`` stub: resolves the daemon unit and reports *installed* properties."""
    def run(argv, **kw):
        if calls is not None:
            calls.append(list(argv))
        if "LoadState" in argv:
            return subprocess.CompletedProcess(argv, 0, f"LoadState=loaded\nId={UNIT}\n", "")
        if "show" in argv:
            return subprocess.CompletedProcess(argv, rc, installed, "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def _unit_file(tmp_path, body="[Service]\nKillMode=mixed\nOOMPolicy=continue\n"):
    path = tmp_path / UNIT
    path.write_text(body)
    return path


def test_an_uninstalled_oom_policy_is_reported(tmp_path):
    """The exact #2702 state: the repo says continue, the host still runs stop."""
    problems = steps.check_daemon_unit_drift(
        _unit_file(tmp_path), run=_systemd("KillMode=mixed\nOOMPolicy=stop\n"))
    assert len(problems) == 1
    assert "OOMPolicy=stop" in problems[0] and "OOMPolicy=continue" in problems[0]
    assert UNIT in problems[0], "the warning must name the unit to fix"


def test_an_installed_unit_that_matches_is_clean(tmp_path):
    assert steps.check_daemon_unit_drift(
        _unit_file(tmp_path), run=_systemd("KillMode=mixed\nOOMPolicy=continue\n")) == []


@pytest.mark.parametrize("run", [
    _systemd("KillMode=mixed\nOOMPolicy=stop\n", rc=1),
    _systemd(""),
])
def test_an_unverifiable_unit_is_skipped(tmp_path, run):
    """No evidence is not drift: a failed or empty ``systemctl show`` reports nothing."""
    assert steps.check_daemon_unit_drift(_unit_file(tmp_path), run=run) == []


def test_a_directive_the_repo_does_not_declare_is_not_compared(tmp_path):
    """Only what the repo commits to is checked — systemd's own default is not a drift."""
    unit = _unit_file(tmp_path, "[Service]\nKillMode=mixed\n")
    assert steps.check_daemon_unit_drift(
        unit, run=_systemd("KillMode=mixed\nOOMPolicy=stop\n")) == []


def test_a_remote_host_is_checked_over_its_ssh_prefix(tmp_path):
    calls: list = []
    prefix = ["ssh", "rocky"]
    steps.check_daemon_unit_drift(_unit_file(tmp_path), prefix=prefix,
                                  run=_systemd("OOMPolicy=stop\n", calls))
    assert calls and all(c[:2] == prefix for c in calls)


@pytest.mark.parametrize("unit_file", [steps.LOCAL_DAEMON_UNIT_FILE,
                                       steps.REMOTE_DAEMON_UNIT_FILE])
def test_the_committed_daemon_units_declare_every_guard(unit_file):
    """A guard the committed unit stops declaring is silently never compared again."""
    declared = steps.service_settings(unit_file.read_text())
    assert all(declared.get(p) for p in steps.DAEMON_UNIT_GUARDS), unit_file.name


ROCKY = R.RunnerTarget(name="rocky", endpoint="https://rocky:9443", enabled=True,
                       healthy=True, ssh_host="rocky", repo_path="/srv/cc")


def _info(sha):
    async def fetch(endpoint):
        return {"sha": sha, "checkoutSha": sha}
    return fetch


class _QuietClient:
    async def call(self, method, **kw):
        return {}

    async def aclose(self):
        pass


def test_a_runner_deploy_carries_its_hosts_drift_without_failing():
    calls: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby",
                        run=_systemd("KillMode=mixed\nOOMPolicy=stop\n", calls),
                        client_factory=lambda e: _QuietClient(),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=False,
                               deps_changed=False, local_daemon_enabled=True))
    assert st.state == "done", "drift is a warning, never a failed runner"
    assert len(st.warnings) == 1 and "OOMPolicy=stop" in st.warnings[0]
    show = [c for c in calls if "OOMPolicy" in c]
    assert show and show[0][0] == "ssh", "a remote runner is checked on ITS host"


@pytest.mark.asyncio
async def test_the_controller_lifts_runner_drift_into_the_deploy_warnings():
    from server.tests.test_deploy_controller import (
        FakeGit, make_controller, make_runners, make_steps)

    g = FakeGit("old", "new", [])
    s, _ = make_steps()
    r, rc = make_runners()
    fan_out = r.deploy_runners

    async def with_drift(targets, **kw):
        results = await fan_out(targets, **kw)
        for p in results.values():
            p.warnings = ["viberpit-daemon.service: installed OOMPolicy=stop"]
        return results
    r.deploy_runners = with_drift

    st = await make_controller(g, s, runners=(r, rc)).deploy_once()
    assert st.error is None
    assert "rocky: viberpit-daemon.service: installed OOMPolicy=stop" in st.warnings
