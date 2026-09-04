from types import SimpleNamespace
from server.deploy import steps
from server.deploy.types import DeployError
import pytest


def make_run(diff_map, fail_cmd=None):
    """diff_map: {path: changed?}. Fakes git-diff and records non-git commands."""
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if len(cmd) > 3 and cmd[:4] == ["git", "-C", cmd[2], "diff"]:
            # last args after '--' are the paths
            paths = cmd[cmd.index("--") + 1:]
            out = "\n".join(p for p in paths if diff_map.get(p))
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        rc = 1 if (fail_cmd and fail_cmd in cmd) else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")
    return run, calls


def test_no_lockfile_change_skips_dep_sync():
    run, calls = make_run({})
    assert steps.sync_deps_if_changed("/r", "a", "b", venv_python="/v/python", run=run) == []
    assert not any("install" in c for c in calls)


def test_pyproject_change_runs_pip_as_a_module_of_the_venv_interpreter():
    """`python -m pip`, never the `bin/pip` launcher: the launcher's shebang bakes in the
    venv's absolute path at creation, so a moved/symlinked venv exec-fails with ENOENT
    (the 2026-08-30 upgrade failure). `bin/python` is a relative symlink and survives."""
    run, calls = make_run({"pyproject.toml": True})
    assert steps.sync_deps_if_changed("/r", "a", "b", venv_python="/v/python", run=run) == ["pip"]
    assert ["/v/python", "-m", "pip", "install", "-e", ".[server,stt]", "--quiet"] in calls
    assert not any(c and c[0].endswith("/pip") for c in calls), "must not exec the pip launcher"


def test_npm_lock_change_runs_npm_ci():
    run, calls = make_run({"web/package-lock.json": True})
    assert steps.sync_deps_if_changed("/r", "a", "b", venv_python="/v/python", run=run) == ["npm"]


def test_pip_failure_raises():
    run, _ = make_run({"uv.lock": True}, fail_cmd="install")
    with pytest.raises(DeployError):
        steps.sync_deps_if_changed("/r", "a", "b", venv_python="/v/python", run=run)


def test_build_failure_raises():
    run, _ = make_run({}, fail_cmd="build")
    with pytest.raises(DeployError):
        steps.build_frontend("/r", run=run)
