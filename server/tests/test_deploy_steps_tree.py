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


def test_fast_forward_to_explicit_sha_merges_that_sha():
    """A hex sha as *ref* fast-forwards to exactly that commit (green-frontier deploys:
    the newest GREEN commit when origin/master's tip is red). The fetch still targets
    the master branch — a sha is not fetchable by name — and the merge names the sha."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    steps.fast_forward("/repo", "abc1234", run=fake_run)
    assert ["git", "-C", "/repo", "fetch", "--quiet", "origin", "master"] in calls, calls
    assert ["git", "-C", "/repo", "merge", "--ff-only", "abc1234"] in calls, calls
    assert not any("origin/abc1234" in c for c in calls), calls


def test_fast_forward_to_sha_stops_short_of_the_tip(tmp_path):
    """Functional: with origin two commits ahead, fast-forwarding to the MIDDLE sha
    lands exactly there — not on the (possibly red) tip."""
    origin = tmp_path / "origin"
    origin.mkdir()

    def go(*a):
        subprocess.run(["git", "-C", str(origin), *a], check=True, capture_output=True, env=_ENV)

    def sha_of(repo):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True, env=_ENV).stdout.strip()

    go("init", "-q", "-b", "master")
    (origin / "a.txt").write_text("1"); go("add", "."); go("commit", "-qm", "c1")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True,
                   capture_output=True, env=_ENV)
    (origin / "a.txt").write_text("2"); go("add", "."); go("commit", "-qm", "c2 (green)")
    middle = sha_of(origin)
    (origin / "a.txt").write_text("3"); go("add", "."); go("commit", "-qm", "c3 (red tip)")

    steps.fast_forward(str(local), middle)

    assert sha_of(local) == middle


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


def _origin_and_clone(tmp_path):
    """Build an origin repo (c1) and a clone of it; return (origin, local, go)."""
    origin = tmp_path / "origin"
    origin.mkdir()

    def go(*a):
        subprocess.run(["git", "-C", str(origin), *a], check=True, capture_output=True, env=_ENV)

    go("init", "-q", "-b", "master")
    (origin / "a.txt").write_text("1"); go("add", "."); go("commit", "-qm", "c1")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True,
                   capture_output=True, env=_ENV)
    return origin, local, go


def test_fast_forward_clears_untracked_file_colliding_with_incoming_tracked_path(tmp_path):
    """Regression (#320 deploy stuck): an untracked working-tree file sharing a path with a
    file newly *tracked* in origin/<ref> aborts `git merge --ff-only`
    ('untracked working tree files would be overwritten by merge'). The deploy host had
    scripts/ccswitch-health-check.sh placed out-of-band before the commit landed → every
    deploy failed identically. The colliding untracked file is safe to drop (origin provides
    the canonical version); fast_forward removes exactly it and retries."""
    origin, local, go = _origin_and_clone(tmp_path)
    # origin commits the script as a TRACKED file.
    (origin / "script.sh").write_text("CANONICAL\n"); go("add", "."); go("commit", "-qm", "c2")
    origin_tip = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, env=_ENV).stdout.strip()
    # local has the same path sitting UNTRACKED with stale content.
    (local / "script.sh").write_text("STALE OUT-OF-BAND\n")

    steps.fast_forward(str(local))

    local_head = subprocess.run(["git", "-C", str(local), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, env=_ENV).stdout.strip()
    assert local_head == origin_tip
    # The canonical tracked version replaced the stale out-of-band file.
    assert (local / "script.sh").read_text() == "CANONICAL\n"


def test_fast_forward_preserves_unrelated_untracked_files(tmp_path):
    """Safety: resolving a collision must NOT touch untracked files that aren't part of the
    incoming commit (the worktree holds precious untracked state: SQLite DB, .venv, web/dist).
    Only the colliding path is removed."""
    origin, local, go = _origin_and_clone(tmp_path)
    (origin / "script.sh").write_text("CANONICAL\n"); go("add", "."); go("commit", "-qm", "c2")
    # local: the colliding untracked file AND an unrelated precious untracked file.
    (local / "script.sh").write_text("STALE\n")
    (local / "ccswitch.db").write_text("PRECIOUS LOCAL STATE\n")

    steps.fast_forward(str(local))

    assert (local / "script.sh").read_text() == "CANONICAL\n"
    # The unrelated untracked file survives untouched.
    assert (local / "ccswitch.db").read_text() == "PRECIOUS LOCAL STATE\n"
