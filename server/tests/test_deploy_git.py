import subprocess
from pathlib import Path
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
