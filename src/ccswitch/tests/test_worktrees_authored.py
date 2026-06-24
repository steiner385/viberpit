import subprocess
from ccswitch import worktrees


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    p = str(path)
    _git(p, "init", "-q", "-b", "master")
    _git(p, "config", "user.email", "t@t.t")
    _git(p, "config", "user.name", "t")
    (path / "f").write_text("x")
    _git(p, "add", ".")
    _git(p, "commit", "-q", "-m", "init")
    return p


def test_collects_feature_branches_filters_scratch_and_default(tmp_path):
    p = _init_repo(tmp_path / "repo")
    # Simulate a session: scratch branch, then two feature branches.
    _git(p, "checkout", "-q", "-b", "ccswitch/master-abcd1234")
    _git(p, "checkout", "-q", "-b", "feat/one")
    _git(p, "checkout", "-q", "-b", "feat/two")
    got = worktrees.authored_branches(p, scratch_branch="ccswitch/master-abcd1234")
    assert got == ["feat/one", "feat/two"]  # sorted; scratch + master filtered


def test_non_git_dir_returns_empty(tmp_path):
    assert worktrees.authored_branches(str(tmp_path)) == []
