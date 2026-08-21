"""#709: the three edge→daemon inspection RPCs, tested at the orchestrator layer.

Each finding moved a host-local inspection (edge /proc, edge filesystem) onto the
daemon. These tests exercise the daemon-side methods directly with real temp dirs,
so they assert the inspection actually happens where the data lives — not just that
the RPC is wired (that is test_remote_orchestrator_breadth.py's job).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest

from server.sessions.adopt import AdoptionCandidate
from server.sessions.orchestrator import SessionOrchestrator


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)


def _cand(sid, **kw):  # untyped body (repo convention) so the dict-merge isn't flagged
    base = dict(pid=5, account="kindash", cwd="/home/u/repo", claude_session_id=sid,
                transcript_path="/x.jsonl", name="Work", mapped=True, tracked=False,
                skip_reason=None, last_activity=datetime.now(timezone.utc), pid_alive=True)
    base.update(kw)
    return AdoptionCandidate(**base)


# --- finding 4: discovery + wire serialization -----------------------------


@pytest.mark.asyncio
async def test_list_adoptable_candidates_serializes_wire_dicts():
    orch = _orch()
    cands = [_cand("aaa"), _cand("bbb", mapped=False, account=None, skip_reason="unmapped")]
    with mock.patch("server.sessions.adopt.discover_candidates", return_value=cands):
        out = await orch.list_adoptable_candidates()
    assert [c["claudeSessionId"] for c in out] == ["aaa", "bbb"]
    assert out[0]["skipReason"] is None and out[1]["skipReason"] == "unmapped"
    # datetime is serialized to an ISO string, not a raw object (JSON-safe over RPC).
    assert isinstance(out[0]["lastActivity"], str)
    assert out[0]["pidAlive"] is True


# --- finding 5: archive-home migration on the daemon's filesystem ----------


@pytest.mark.asyncio
async def test_migrate_archive_home_moves_dirs(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "s1").mkdir(parents=True)
    (old / "s1" / "meta.json").write_text("{}")
    (old / "s2").mkdir()
    # s3 is in the DB list but has no dir on disk — must be skipped, not counted.
    result = await _orch().migrate_archive_home(str(old), str(new), ["s1", "s2", "s3"])
    assert result == {"moved": 2, "failures": 0}
    assert (new / "s1" / "meta.json").read_text() == "{}"
    assert not (old / "s1").exists()


@pytest.mark.asyncio
async def test_migrate_archive_home_noop_when_same_dir(tmp_path):
    result = await _orch().migrate_archive_home(str(tmp_path), str(tmp_path), ["s1"])
    assert result == {"moved": 0, "failures": 0}


@pytest.mark.asyncio
async def test_migrate_archive_home_skips_existing_dest(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "s1").mkdir(parents=True)
    (new / "s1").mkdir(parents=True)  # collision: never overwrite
    result = await _orch().migrate_archive_home(str(old), str(new), ["s1"])
    assert result == {"moved": 0, "failures": 0}
    assert (old / "s1").exists()  # source left intact


# --- finding 6: clone presence on the daemon's clones base -----------------


@pytest.mark.asyncio
async def test_cloned_repos_filters_by_git_presence(tmp_path):
    (tmp_path / "o" / "present" / ".git").mkdir(parents=True)
    (tmp_path / "o" / "nogit").mkdir(parents=True)  # dir exists but no .git
    with mock.patch("ccswitch.gitrepos.clones_base", return_value=tmp_path):
        out = await _orch().cloned_repos(["o/present", "o/nogit", "o/absent", "malformed"])
    assert out == ["o/present"]
