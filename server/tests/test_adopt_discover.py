"""discover_candidates: account mapping, transcript resolution, flagging."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from server.sessions.adopt import ProcInfo, discover_candidates


def _make_transcript(cfg: Path, project: str, sid: str, title="My Work") -> Path:
    pdir = cfg / "projects" / project
    pdir.mkdir(parents=True, exist_ok=True)
    f = pdir / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "summary", "summary": title}) + "\n")
    return f


def test_discover_maps_account_and_resolves_transcript(tmp_path):
    cfg = tmp_path / "cfg" / "kindash"
    cwd = "/home/u/repo"
    sid = "11111111-1111-1111-1111-111111111111"
    _make_transcript(cfg, "-home-u-repo", sid)
    procs = [ProcInfo(pid=42, cmdline=["claude", "agents"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd=cwd)]
    cands = discover_candidates(
        known_accounts=["kindash"], tracked_sids=set(),
        proc_lister=lambda: procs,
        account_cfg=lambda name: cfg if name == "kindash" else tmp_path / name,
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.account == "kindash" and c.mapped and not c.tracked
    assert c.claude_session_id == sid and c.skip_reason is None
    assert c.pid == 42 and c.pid_alive
    assert isinstance(c.last_activity, datetime)


def test_discover_flags_unmapped_config_dir(tmp_path):
    cfg = tmp_path / "dot-claude"  # not a managed account dir
    sid = "22222222-2222-2222-2222-222222222222"
    _make_transcript(cfg, "-home-u-repo", sid)
    procs = [ProcInfo(pid=7, cmdline=["claude"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd="/home/u/repo")]
    cands = discover_candidates(
        known_accounts=["kindash"], tracked_sids=set(), proc_lister=lambda: procs,
        account_cfg=lambda name: tmp_path / "cfg" / name)
    assert len(cands) == 1
    assert not cands[0].mapped and cands[0].skip_reason == "unmapped"


def test_discover_flags_already_tracked(tmp_path):
    cfg = tmp_path / "cfg" / "kindash"
    sid = "33333333-3333-3333-3333-333333333333"
    _make_transcript(cfg, "-home-u-repo", sid)
    procs = [ProcInfo(pid=9, cmdline=["claude", "agents"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd="/home/u/repo")]
    cands = discover_candidates(
        known_accounts=["kindash"], tracked_sids={sid}, proc_lister=lambda: procs,
        account_cfg=lambda name: cfg if name == "kindash" else tmp_path / name)
    assert cands[0].tracked and cands[0].skip_reason == "tracked"


def test_discover_excludes_sdk_and_helper(tmp_path):
    procs = [
        ProcInfo(pid=1, cmdline=["claude", "--input-format", "stream-json"], environ={}, cwd="/x"),
        ProcInfo(pid=2, cmdline=["claude", "--bg-spare", "/s"], environ={}, cwd="/x"),
    ]
    cands = discover_candidates(["kindash"], set(), proc_lister=lambda: procs,
                                account_cfg=lambda n: tmp_path / n)
    assert cands == []


def test_discover_excludes_unresolvable_transcript(tmp_path):
    cfg = tmp_path / "cfg" / "kindash"
    cfg.mkdir(parents=True)
    procs = [ProcInfo(pid=3, cmdline=["claude", "agents"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd="/home/u/repo")]
    cands = discover_candidates(["kindash"], set(), proc_lister=lambda: procs,
                                account_cfg=lambda name: cfg)
    assert cands == []
