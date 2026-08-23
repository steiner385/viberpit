"""Sub-agent sidecar discovery must be bounded (RC2).

The old implementation globbed `*/<sid>/subagents/*.meta.json` across every
project directory — 1,696 of them on the live fleet, over an sshfs mount at
~54ms RTT — which hung transcript_window for 30s+ and made sessions
un-openable. Discovery must degrade to a partial result instead of hanging.
"""
import time
from pathlib import Path

import pytest

from server.sessions.replay import _find_subagent_meta_files


def _layout(root: Path, sid: str, *, cwd_dirs: list[str], sub_in: str) -> Path:
    """Build projects/<cwd_dirs...>/ with the sidecar under `sub_in`."""
    projects = root / "projects"
    for d in cwd_dirs:
        (projects / d).mkdir(parents=True, exist_ok=True)
    sub = projects / sub_in / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-1.meta.json").write_text('{"toolUseId": "toolu_task"}')
    return projects


def test_finds_sidecar_in_the_sessions_own_project_dir(tmp_path):
    projects = _layout(tmp_path, "s1", cwd_dirs=["-cwd-main", "-cwd-other"], sub_in="-cwd-main")
    metas, complete = _find_subagent_meta_files(projects, "s1", preferred=projects / "-cwd-main")
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True


def test_finds_sidecar_under_a_different_cwd(tmp_path):
    """The cross-cwd join (main checkout vs. worktree) must keep working."""
    projects = _layout(tmp_path, "s2", cwd_dirs=["-cwd-main", "-cwd-worktree"], sub_in="-cwd-worktree")
    metas, complete = _find_subagent_meta_files(projects, "s2", preferred=projects / "-cwd-main")
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True


def test_missing_sidecar_returns_empty_not_error(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-cwd-main").mkdir(parents=True)
    metas, complete = _find_subagent_meta_files(projects, "nope", preferred=projects / "-cwd-main")
    assert metas == []
    assert complete is True


def test_slow_filesystem_degrades_instead_of_hanging(tmp_path, monkeypatch):
    """THE regression test for RC2.

    Simulate the sshfs round-trip cost. With a 0.2s budget and 50ms per
    directory probe, discovery must return in well under the time a full
    traversal would take, and report itself incomplete.
    """
    projects = _layout(
        tmp_path, "s3",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-main",
    )

    real_is_dir = Path.is_dir

    def slow_is_dir(self):
        time.sleep(0.05)          # stands in for one sshfs round trip
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)

    started = time.monotonic()
    metas, complete = _find_subagent_meta_files(
        projects, "s3", preferred=projects / "-cwd-main", budget_s=0.2)
    elapsed = time.monotonic() - started

    # A full 61-directory traversal would cost ~3s. The budget must cut it short.
    assert elapsed < 1.0, f"discovery took {elapsed:.2f}s — budget not enforced"
    assert complete is False, "a truncated scan must report itself incomplete"
    # The fast path still delivered the sidecar that actually matters.
    assert [p.name for p in metas] == ["agent-1.meta.json"]
