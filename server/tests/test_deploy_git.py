import subprocess
import pytest
from server.deploy import git


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def g(*a):
        subprocess.run(["git", "-C", str(r), *a], check=True,
                       capture_output=True, env={**__import__("os").environ, **env})
    g("init", "-q")
    (r / "a.txt").write_text("1")
    g("add", "."); g("commit", "-qm", "first")
    return r


def test_current_sha_is_40_hex(repo):
    sha = git.current_sha(str(repo))
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_abs_git_dir_points_into_repo(repo):
    assert git.abs_git_dir(str(repo)).startswith(str(repo))


def test_is_ancestor(repo):
    base = git.current_sha(str(repo))
    (repo / "b.txt").write_text("2")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second"], check=True,
                   capture_output=True,
                   env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})
    head = git.current_sha(str(repo))
    assert git.is_ancestor(str(repo), base, head) is True
    assert git.is_ancestor(str(repo), head, base) is False
    assert git.is_ancestor(str(repo), head, head) is True, "a commit is its own ancestor"
    # An unknown rev is "not an ancestor", never an exception — preflight turns
    # this into a clean DeployError instead of a stack trace.
    assert git.is_ancestor(str(repo), "0" * 40, head) is False


def _no_username():
    # The exact failure a `gh auth git-credential` keyring timeout produces: gh prints
    # nothing, git tries to prompt with no tty, and dies with 128.
    return subprocess.CalledProcessError(
        128, ["git", "fetch"],
        stderr="fatal: could not read Username for 'https://github.com': "
               "No such device or address\n")


def _scripted_run(outcomes):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run, calls


def test_fetch_retries_a_transient_failure_then_succeeds():
    """A credential-helper blip fails one `git fetch` and clears seconds later. Preflight
    ran the fetch once, so a single blip aborted the whole deploy (2026-09-11 06:36 EDT,
    and twice on 2026-09-09). Retry with backoff rides it out."""
    run, calls = _scripted_run([_no_username(), _no_username(), None])
    naps: list[float] = []
    git.fetch("/r", run=run, sleep=naps.append)
    assert len(calls) == 3 and all(a[:4] == ["git", "-C", "/r", "fetch"] for a in calls)
    assert naps == [2.0, 4.0], "backs off between attempts, never after success"


def test_fetch_reraises_once_attempts_are_exhausted():
    run, calls = _scripted_run([_no_username(), _no_username(), _no_username()])
    naps: list[float] = []
    with pytest.raises(subprocess.CalledProcessError) as ei:
        git.fetch("/r", run=run, sleep=naps.append)
    assert "could not read Username" in ei.value.stderr, "the original error surfaces"
    assert len(calls) == 3
    assert naps == [2.0, 4.0], "never sleeps after the final attempt"


def test_fetch_succeeds_first_try_without_sleeping():
    run, calls = _scripted_run([None])
    naps: list[float] = []
    git.fetch("/r", ref="main", run=run, sleep=naps.append)
    assert calls == [["git", "-C", "/r", "fetch", "--quiet", "origin", "main"]]
    assert naps == []


def test_log_range_lists_new_commits(repo):
    base = git.current_sha(str(repo))
    (repo / "b.txt").write_text("2")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second body"], check=True,
                   capture_output=True,
                   env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})
    head = git.current_sha(str(repo))
    rows = git.log_range(str(repo), base, head)
    assert len(rows) == 1 and rows[0]["subject"] == "second body"
    assert rows[0]["sha"].startswith(head[:7])
