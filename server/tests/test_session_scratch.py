"""The per-session scratch dir that survives daemon restarts (#2910)."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from server.sessions.session_scratch import SCRATCH_DIRNAME, scratch_dir_for
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.tools import build_session_tools


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def linked_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "s1", str(wt), cwd=repo)
    return repo, wt


def test_scratch_lives_in_the_worktree_git_dir_and_stays_out_of_status(linked_worktree):
    repo, wt = linked_worktree
    path = scratch_dir_for(str(wt), "sess-1")
    assert path is not None and os.path.isdir(path)
    assert path.startswith(os.path.realpath(repo / ".git" / "worktrees"))
    assert path.endswith(os.path.join(SCRATCH_DIRNAME, "sess-1"))
    with open(os.path.join(path, "run.log"), "w") as fh:
        fh.write("log")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True,
                            text=True, check=True).stdout
    assert status == ""
    assert scratch_dir_for(str(wt), "sess-1") == path          # idempotent; content kept
    assert os.path.exists(os.path.join(path, "run.log"))


def test_scratch_goes_with_the_worktree(linked_worktree):
    repo, wt = linked_worktree
    path = scratch_dir_for(str(wt), "sess-1")
    _git("worktree", "remove", "--force", str(wt), cwd=repo)
    assert not os.path.exists(path)


def test_no_scratch_outside_git_or_for_a_bad_id(tmp_path, linked_worktree):
    _, wt = linked_worktree
    assert scratch_dir_for(str(tmp_path / "plain"), "s") is None
    assert scratch_dir_for(None, "s") is None
    for bad in ("", "..", "a/b"):
        assert scratch_dir_for(str(wt), bad) is None


@pytest.mark.asyncio
async def test_session_context_reports_scratch_dir(linked_worktree):
    _, wt = linked_worktree
    rec = SimpleNamespace(id="s1", name="S", cwd=str(wt), parent_session_id=None)
    ctx = SessionToolContext(session_id="s1", identity="u@x", kind=None, engine="claude_code",
                             record=lambda: rec, host="dobby")
    tool = next(t for t in build_session_tools(ctx) if t.name == "session_context")
    data = json.loads((await tool.handler({}))["content"][0]["text"])["data"]
    assert data["scratchDir"].endswith(os.path.join(SCRATCH_DIRNAME, "s1"))
