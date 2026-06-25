import os, subprocess
import pytest
from server.deploy import steps
from server.deploy.types import DeployError

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"; r.mkdir()
    def g(*a): subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, env=_ENV)
    g("init", "-q"); (r / "a.txt").write_text("1"); g("add", "."); g("commit", "-qm", "first")
    return r


def test_guard_discards_tracked_edit(repo):
    (repo / "a.txt").write_text("DIRTY")
    assert steps.guard_working_tree(str(repo)) is True
    assert (repo / "a.txt").read_text() == "1"


def test_guard_clean_tree_is_noop(repo):
    assert steps.guard_working_tree(str(repo)) is False


def test_fast_forward_failure_raises_deploy_error(repo):
    # No 'origin' remote → pull fails → DeployError(update).
    with pytest.raises(DeployError) as ei:
        steps.fast_forward(str(repo))
    assert ei.value.phase == "update"
