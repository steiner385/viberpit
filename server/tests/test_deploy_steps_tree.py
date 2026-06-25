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
    # No 'origin' remote → fetch fails → DeployError(update).
    with pytest.raises(DeployError) as ei:
        steps.fast_forward(str(repo))
    assert ei.value.phase == "update"


def test_fast_forward_merges_tracking_ref_not_fetch_head():
    """Regression: the deploy's `git pull --ff-only origin master` merges via the
    shared FETCH_HEAD, which the DetectLoop's concurrent 30s `git fetch origin master`
    duplicates (two for-merge lines) → `git merge --ff-only` fails with
    'Cannot fast-forward to multiple branches'. Fix: fast-forward the unambiguous
    tracking ref origin/<ref>, never FETCH_HEAD.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    steps.fast_forward("/repo", run=fake_run)
    # Must NOT use the FETCH_HEAD-merging pull (the racy path).
    assert not any("pull" in c for c in calls), calls
    # Must fast-forward the single tracking ref instead.
    assert ["git", "-C", "/repo", "merge", "--ff-only", "origin/master"] in calls, calls


def test_fast_forward_advances_repo_behind_origin(tmp_path):
    """Functional: a clone one commit behind origin/master is fast-forwarded to the tip."""
    origin = tmp_path / "origin"
    origin.mkdir()

    def go(*a):
        subprocess.run(["git", "-C", str(origin), *a], check=True, capture_output=True, env=_ENV)

    go("init", "-q", "-b", "master")
    (origin / "a.txt").write_text("1"); go("add", "."); go("commit", "-qm", "c1")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True,
                   capture_output=True, env=_ENV)
    # Advance origin past the clone.
    (origin / "a.txt").write_text("2"); go("add", "."); go("commit", "-qm", "c2")
    origin_tip = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, env=_ENV).stdout.strip()

    steps.fast_forward(str(local))

    local_head = subprocess.run(["git", "-C", str(local), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, env=_ENV).stdout.strip()
    assert local_head == origin_tip
