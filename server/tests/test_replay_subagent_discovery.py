"""Sub-agent sidecar discovery must be bounded (RC2).

The old implementation globbed `*/<sid>/subagents/*.meta.json` across every
project directory — 1,696 of them on the live fleet, over an sshfs mount at
~54ms RTT — which hung transcript_window for 30s+ and made sessions
un-openable. Discovery must degrade to a partial result instead of hanging.
"""
import os
import time
from pathlib import Path

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


def test_flaky_entry_during_scan_does_not_abort_and_reports_incomplete(tmp_path, monkeypatch):
    """A per-entry OSError mid-scan (a stale/flaky sshfs handle on exactly one of
    the 1,696 project dirs is the plausible real-world trigger) must not abort the
    sweep, and the scan must NOT be reported as complete=True — a falsely-True
    complete flag would defeat the Task 2 memoization guard (never cache a
    partial result)."""
    projects = _layout(
        tmp_path, "s4",
        cwd_dirs=["-cwd-flaky", "-cwd-other"],
        sub_in="-cwd-main",
    )

    real_is_dir = os.DirEntry.is_dir

    def flaky_is_dir(self):
        if self.name == "-cwd-flaky":
            raise OSError("stale sshfs handle")
        return real_is_dir(self)

    monkeypatch.setattr(os.DirEntry, "is_dir", flaky_is_dir)

    metas, complete = _find_subagent_meta_files(projects, "s4", preferred=projects / "-cwd-main")

    # The fast path (preferred dir) still delivered the sidecar.
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    # But the sweep hit an error on one entry, so it cannot be trusted as exhaustive.
    assert complete is False


def test_collect_oserror_on_preferred_dir_marks_incomplete(tmp_path, monkeypatch):
    """A per-directory OSError raised inside `_collect` itself (e.g. `sub.is_dir()`
    failing on a stale sshfs handle for the probed `<cwd>/<sid>/subagents` dir) must
    mark the overall result incomplete — not be silently swallowed as if that
    directory had been fully scanned. This is the same defect class as the
    flaky-entry test above, at a different site (inside `_collect`, not the outer
    scandir loop), and it must be fixed before the discovery cache is trustworthy:
    a falsely-True `complete` here would let a wrong answer live for the whole
    cache TTL."""
    projects = _layout(tmp_path, "s6", cwd_dirs=["-cwd-main"], sub_in="-cwd-main")

    real_is_dir = Path.is_dir

    def raising_is_dir(self):
        if self.name == "subagents":
            raise OSError("stale sshfs handle")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", raising_is_dir)

    metas, complete = _find_subagent_meta_files(projects, "s6", preferred=projects / "-cwd-main")

    assert metas == []
    assert complete is False


def test_second_lookup_does_not_rescan(tmp_path, monkeypatch):
    from server.sessions import replay

    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s4", cwd_dirs=["-cwd-main", "-cwd-b"], sub_in="-cwd-main")

    calls = {"n": 0}
    real_scandir = os.scandir

    def counting_scandir(path):
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(replay.os, "scandir", counting_scandir)

    a, _ = replay._find_subagent_meta_files(projects, "s4", preferred=projects / "-cwd-main")
    after_first = calls["n"]
    b, _ = replay._find_subagent_meta_files(projects, "s4", preferred=projects / "-cwd-main")

    assert a == b
    assert after_first >= 1, "the first lookup should have scanned"
    assert calls["n"] == after_first, "the second lookup must be served from cache"


def test_partial_results_are_not_cached(tmp_path, monkeypatch):
    """A budget-truncated scan must not become permanent."""
    from server.sessions import replay

    replay._subagent_cache_clear()
    projects = _layout(
        tmp_path, "s5",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-main",
    )

    real_is_dir = Path.is_dir

    def slow_is_dir(self):
        time.sleep(0.05)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)
    _, complete = replay._find_subagent_meta_files(
        projects, "s5", preferred=projects / "-cwd-main", budget_s=0.2)
    assert complete is False

    monkeypatch.undo()
    calls = {"n": 0}
    real_scandir = os.scandir

    def counting_scandir(path):
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(replay.os, "scandir", counting_scandir)
    _, complete2 = replay._find_subagent_meta_files(
        projects, "s5", preferred=projects / "-cwd-main")

    assert calls["n"] >= 1, "a partial result must not have been cached"
    assert complete2 is True


# ---------------------------------------------------------------------------
# Truncation logging severity (review Fix 1)
#
# The severity must key off EVIDENCE that sub-agents exist, not off the fast
# path coming up empty. The original condition was inverted: on the live fleet
# zero `subagents/` dirs exist, so `found` is empty on essentially every call —
# warning-on-empty flooded the log on every single transcript request, which is
# the exact flood the branch was written to prevent.
# ---------------------------------------------------------------------------


def _slow_scan(tmp_path, monkeypatch, sid, *, with_sidecar: bool):
    """Force a budget-truncated scan, with or without a discoverable sidecar."""
    from server.sessions import replay

    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    for i in range(60):
        (projects / f"-cwd-{i}").mkdir(parents=True, exist_ok=True)
    (projects / "-cwd-main").mkdir(parents=True, exist_ok=True)
    if with_sidecar:
        sub = projects / "-cwd-main" / sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "agent-1.meta.json").write_text('{"toolUseId": "toolu_task"}')

    real_is_dir = Path.is_dir

    def slow_is_dir(self):
        time.sleep(0.05)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)
    return projects


def test_truncated_scan_with_no_sidecars_does_not_warn(tmp_path, monkeypatch, caplog):
    """The steady state on the live fleet: nothing found, scan truncated.

    No evidence sub-agents exist at all → debug, NOT warning. A warning here
    fires on every session open and every scrollback page — a pure log flood.
    """
    import logging

    projects = _slow_scan(tmp_path, monkeypatch, "s-quiet", with_sidecar=False)
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    metas, complete = _find_subagent_meta_files(
        projects, "s-quiet", preferred=projects / "-cwd-main", budget_s=0.2)

    assert metas == []
    assert complete is False, "the scan must actually have truncated for this test to mean anything"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"truncation with no sidecars must not warn, got: {[r.getMessage() for r in warnings]}"
    assert any(r.levelno == logging.DEBUG and "budget exhausted" in r.getMessage()
               for r in caplog.records), "it should still be visible at debug"


def test_truncated_scan_with_sidecars_found_warns(tmp_path, monkeypatch, caplog):
    """Sidecars demonstrably exist AND the sweep was cut short — the cross-cwd
    directories we never reached may hold more. That IS actionable, so warn."""
    import logging

    projects = _slow_scan(tmp_path, monkeypatch, "s-loud", with_sidecar=True)
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    metas, complete = _find_subagent_meta_files(
        projects, "s-loud", preferred=projects / "-cwd-main", budget_s=0.2)

    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "a truncated scan that DID find sidecars must warn exactly once"
    assert "may be incomplete" in warnings[0].getMessage()
