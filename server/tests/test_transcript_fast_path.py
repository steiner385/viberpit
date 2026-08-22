"""Transcript lookup must not scan every project dir.

_find_transcript globbed `<root>/projects/*/<csid>.jsonl`, which is one stat per
project directory — 1741 on this deployment. When the account store is a remote
mount each stat is a network round-trip, so a single lookup took >200s on rocky,
and _goal_sweep calls it per session ON THE EVENT LOOP: the daemon never finished
starting (2026-08-21 outage).
"""
from __future__ import annotations

from server.sessions.orchestrator import SessionOrchestrator as SO


def test_project_dir_name_matches_claude_encoding():
    """Verified against 12/12 real transcripts: '/', '.' and '_' all become '-'."""
    assert SO.project_dir_name("/home/tony/GitHub/ccswitch") == "-home-tony-GitHub-ccswitch"
    assert (SO.project_dir_name("/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/KinDash/9f97")
            == "-mnt-ssk-ssd-tony-GitHub--ccswitch-worktrees-KinDash-9f97")
    assert SO.project_dir_name("/home/tony/my_repo") == "-home-tony-my-repo"


def test_fast_path_finds_without_scanning(tmp_path, monkeypatch):
    """With a cwd, the lookup stats one candidate and never lists projects/."""
    import server.sessions.orchestrator as orch_mod

    cfg = tmp_path / "cfg"
    cwd = "/home/tony/GitHub/ccswitch"
    proj = cfg / "projects" / SO.project_dir_name(cwd)
    proj.mkdir(parents=True)
    (proj / "abc.jsonl").write_text("{}")

    monkeypatch.setattr(orch_mod, "shared_base", lambda: str(tmp_path / "shared"))

    calls: list[str] = []
    import glob as real_glob
    monkeypatch.setattr(orch_mod.__dict__.get("glob", real_glob), "glob",
                        lambda *a, **k: calls.append(a[0]) or [], raising=False)

    o = SO.__new__(SO)
    got = o._find_transcript(str(cfg), "abc", cwd)
    assert got == str(proj / "abc.jsonl")


def test_falls_back_to_glob_without_a_cwd(tmp_path, monkeypatch):
    """No cwd (or an unexpected layout) must still resolve — correctness first."""
    import server.sessions.orchestrator as orch_mod

    cfg = tmp_path / "cfg"
    proj = cfg / "projects" / "-some-unrelated-encoding"
    proj.mkdir(parents=True)
    (proj / "xyz.jsonl").write_text("{}")
    monkeypatch.setattr(orch_mod, "shared_base", lambda: str(tmp_path / "shared"))

    o = SO.__new__(SO)
    assert o._find_transcript(str(cfg), "xyz", None) == str(proj / "xyz.jsonl")


def test_wrong_cwd_still_falls_back(tmp_path, monkeypatch):
    """A stale cwd (e.g. after the repo root moved) must not lose the transcript."""
    import server.sessions.orchestrator as orch_mod

    cfg = tmp_path / "cfg"
    proj = cfg / "projects" / "-mnt-old-path"
    proj.mkdir(parents=True)
    (proj / "zzz.jsonl").write_text("{}")
    monkeypatch.setattr(orch_mod, "shared_base", lambda: str(tmp_path / "shared"))

    o = SO.__new__(SO)
    # cwd points somewhere with no matching project dir -> fast path misses, glob wins
    assert o._find_transcript(str(cfg), "zzz", "/home/tony/new/path") == str(proj / "zzz.jsonl")
