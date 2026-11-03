"""Rewind to here (D-9): ``rewind_session`` / ``undo_rewind`` on the orchestrator and
every layer above it (daemon RPC, RemoteOrchestrator, fleet router, facade, the two
``POST /api/sessions/{id}/rewind[/undo]`` routes).

The REAL orchestrator and the REAL Claude driver run on a real temp git worktree, a
real CLI-shaped transcript (the D-9 spike's row shapes: prompts, replies,
``file-history-snapshot`` / ``-delta`` rows, the rewind's appended ``last-prompt``
rows) and a real SQLite DB. Only the CLI is faked: ``FakeCli`` stands in for the SDK's
``Query._send_control_request`` and does what the CLI does on disk — restores the
files on ``rewind_files`` and appends the rewind rows on ``rewind_conversation`` — with
per-test overrides. No ``claude`` ever runs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from viberpit import fs_beneath
from viberpit.selector import Picked
from server.app import create_app
from server.db import db_session, dispose_db, init_db
from server.db.models import SessionRewind
from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import events as ev
from server.sessions import rewind as rw
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import DRIVER_SWAP_TIMEOUT_S, SessionOrchestrator
from server.sessions.remote import REWIND_RPC_TIMEOUT, SPAWN_RPC_TIMEOUT, RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.ipc_harness import serve_daemon_uds

_H = {"Tailscale-User-Login": "testuser@example.com"}
_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

SDK_SID = "src-sdk"
U1, U2, U3 = ("21203c3a-0000-4000-8000-000000000001", "0497e020-0000-4000-8000-000000000002",
              "2ff905f3-0000-4000-8000-000000000003")
A1, A2, A3 = ("9346e4fc-0000-4000-8000-0000000000a1", "fa035a71-0000-4000-8000-0000000000a2",
              "aa40d77f-0000-4000-8000-0000000000a3")
PROMPTS = {U1: "first prompt", U2: "second prompt", U3: "third prompt"}
PRECEDING = {U1: None, U2: A1, U3: A2}
#: Live (driver-minted) user turn ids, as the record holds them after the echo.
LIVE = {"live-1": U1, "live-2": U2, "live-3": U3}
#: A clean undo's answer.
OK = {"ok": True, "skipped": []}


def _git(cwd: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_GIT_ENV}).stdout


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from viberpit.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _snapshot(msg: str, tracked: dict[str, str | None]) -> dict:
    return {"type": "file-history-snapshot", "messageId": msg, "isSnapshotUpdate": False,
            "snapshot": {"messageId": msg, "timestamp": "2026-11-03T11:12:42.834Z",
                         "trackedFileBackups": {
                             p: {"backupFileName": b, "version": 1,
                                 "backupTime": "2026-11-03T11:12:43.880Z"}
                             for p, b in tracked.items()}}}


def _delta(snapshot_msg: str, path: str, backup: str | None) -> dict:
    return {"type": "file-history-delta", "messageId": str(uuid.uuid4()),
            "snapshotMessageId": snapshot_msg, "trackingPath": path,
            "backup": {"backupFileName": backup, "version": 1,
                       "backupTime": "2026-11-03T11:12:44.110Z"}}


def _user(u: str, parent: str | None) -> dict:
    return {"type": "user", "uuid": u, "parentUuid": parent, "sessionId": SDK_SID,
            "message": {"role": "user", "content": PROMPTS[u]}}


def _assistant(a: str, parent: str, text: str) -> dict:
    return {"type": "assistant", "uuid": a, "parentUuid": parent, "sessionId": SDK_SID,
            "message": {"role": "assistant", "model": "m",
                        "content": [{"type": "text", "text": text}]}}


def _transcript_rows() -> list[dict]:
    """Three turns. At U2's snapshot ``a.txt``, ``mode.sh`` and ``gone.txt`` existed;
    T2 created ``new.txt`` (null backup = did not exist), chmod'ed ``mode.sh`` and
    Bash removed ``gone.txt``; T3 edited ``a.txt`` again."""
    return [
        _user(U1, None), _snapshot(U1, {}), _assistant(A1, U1, "r1"),
        _delta(U1, "a.txt", "a@v1"),
        _user(U2, A1),
        _snapshot(U2, {"a.txt": "a@v2", "mode.sh": "m@v1", "gone.txt": "g@v1"}),
        _assistant(A2, U2, "r2"),
        _delta(U2, "new.txt", None),
        _user(U3, A2), _snapshot(U3, {"a.txt": "a@v3", "new.txt": "n@v2"}),
        _assistant(A3, U3, "r3"),
        {"type": "last-prompt", "lastPrompt": PROMPTS[U3], "leafUuid": A3, "sessionId": SDK_SID},
    ]


def _write_transcript(cwd: str) -> Path:
    d = (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects"
         / SessionOrchestrator.project_dir_name(cwd))
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{SDK_SID}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in _transcript_rows()))
    return path


#: The worktree "now" (after T3) and as it was at U2's checkpoint.
NOW = {"a.txt": ("v3\n", 0o644), "new.txt": ("hello\n", 0o644),
       "mode.sh": ("#!/bin/sh\n", 0o755), "b.txt": ("bash\n", 0o644)}
AT_U2 = {"a.txt": ("v1\n", 0o644), "mode.sh": ("#!/bin/sh\n", 0o644),
         "gone.txt": ("old\n", 0o644)}


def _state(cwd: str) -> dict[str, tuple[str, int]]:
    out = {}
    for name in ("a.txt", "new.txt", "mode.sh", "b.txt", "gone.txt"):
        p = Path(cwd, name)
        if p.exists():
            out[name] = (p.read_text(), stat.S_IMODE(p.stat().st_mode))
    return out


class FakeCli:
    """The CLI's control channel, shared by every client the fake factory builds (the
    process the undo resumes included). By default it behaves like CLI 2.1.317 for a
    target with a checkpoint: the dry run lists the content changes (NOT the mode-only
    one), the real ``rewind_files`` restores the U2 state on disk, and
    ``rewind_conversation`` appends the rewind rows. ``override[kind]`` replaces one
    handler (``dry`` / ``files`` / ``conv``)."""

    def __init__(self) -> None:
        self.cwd = ""
        self.transcript: Path | None = None
        self.sent: list[dict] = []
        self.log: list[str] = []
        self.conv_saw_len: int | None = None
        self.override: dict[str, Callable[[dict], Any]] = {}

    async def _send_control_request(self, request: dict, timeout: float = 60.0) -> dict:
        self.sent.append(request)
        if request["subtype"] == "rewind_files":
            kind = "dry" if request.get("dry_run") else "files"
        else:
            kind = "conv"
        self.log.append(kind)
        handler = self.override.get(kind) or getattr(self, f"_{kind}")
        out = handler(request)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    def _dry(self, request: dict) -> dict:
        return {"canRewind": True, "insertions": 1, "deletions": 2,
                "filesChanged": [os.path.join(self.cwd, "a.txt"),
                                 os.path.join(self.cwd, "new.txt"),
                                 os.path.join(self.cwd, "gone.txt")]}

    def apply_files(self) -> None:
        for name, (text, mode) in AT_U2.items():
            p = Path(self.cwd, name)
            p.write_text(text)
            os.chmod(p, mode)
        Path(self.cwd, "new.txt").unlink(missing_ok=True)

    def _files(self, request: dict) -> dict:
        self.apply_files()
        return {"canRewind": True, "skippedLinks": 0}

    def apply_conv(self, target: str) -> None:
        assert self.transcript is not None
        with self.transcript.open("a") as fh:
            for row in ({"type": "last-prompt", "leafUuid": PRECEDING[target],
                         "explicit": True, "rewound": True, "cutUuid": target,
                         "sessionId": SDK_SID},
                        {"type": "last-prompt", "lastPrompt": PROMPTS[U3],
                         "leafUuid": PRECEDING[target], "sessionId": SDK_SID},
                        {"type": "cost-state", "totalCostUSD": 0.004}):
                fh.write(json.dumps(row) + "\n")

    def active_prompts(self) -> list[str]:
        """The prompts on the transcript's active chain, as the CLI sees them: every
        user row in order, minus each rewind row's cut prompt and those after it."""
        assert self.transcript is not None
        prompts: list[str] = []
        for line in self.transcript.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "user":
                prompts.append(row["uuid"])
            elif row.get("type") == "last-prompt" and row.get("rewound"):
                cut = row["cutUuid"]
                if cut in prompts:
                    del prompts[prompts.index(cut):]
        return prompts

    def _conv(self, request: dict) -> dict:
        assert self.transcript is not None
        target = request["target_message_uuid"]
        active = self.active_prompts()
        if target not in active:
            return {"rewound": False, "reason": "target_not_found", "error": "not found"}
        # The CLI refuses a cut whose caller has not seen the newest prompt (spike §3).
        if request.get("last_seen_user_message_uuid") != active[-1]:
            return {"rewound": False, "reason": "stale_target", "error": "unseen later turn"}
        self.conv_saw_len = self.transcript.stat().st_size
        self.apply_conv(target)
        return {"rewound": True, "targetMessageUuid": target, "prefillText": PROMPTS[target],
                "precedingAssistantUuid": PRECEDING[target]}

    def kinds(self) -> list[str]:
        return list(self.log)


class Spawns:
    """A fake SDK factory: records each spawn and wires the shared ``FakeCli`` in."""

    def __init__(self, cli: FakeCli) -> None:
        self.cli = cli
        self.calls: list[dict] = []
        self.clients: list[FakeSDKClient] = []

    def __call__(self, cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                 on_pre_compact=None, options_overlay=None, **kw):
        self.calls.append({"cwd": cwd, "config_dir": config_dir, "resume": resume, **kw})
        client = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        client._query = self.cli    # type: ignore[attr-defined]
        self.clients.append(client)
        return client


async def _no_title(*a, **k):
    return None


class Env:
    def __init__(self, orch, spawns, cli, src, transcript, compose_patch, tmp) -> None:
        self.orch: SessionOrchestrator = orch
        self.spawns: Spawns = spawns
        self.cli: FakeCli = cli
        self.src: SessionRecord = src
        self.transcript: Path = transcript
        self.compose = compose_patch
        self.tmp: Path = tmp

    @property
    def cwd(self) -> str:
        return self.src.cwd  # type: ignore[return-value]


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """An orchestrator on the fake CLI, one session in its own git worktree with the
    files as after T3, its transcript, and live prompt uuids for its three turns."""
    store = tmp_path / "store"; store.mkdir()
    shared = tmp_path / "dot-claude"; (shared / "projects").mkdir(parents=True)
    (shared / "settings.json").write_text("{}")
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    (cfg_dir / ".claude.json").write_text("{}")
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    clone = repo_root / "myrepo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    (clone / "README.md").write_text("hi\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "init")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'rewind.db'}"
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    patches: list = [
        patch.dict(os.environ, {"VIBERPIT_HOME": str(store), "CLAUDE_CONFIG_DIR": str(shared),
                                "VIBERPIT_DB_URL": db_url}),
        compose_patch,
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
        # The first prompt auto-titles the session: never a real model call here.
        patch("server.sessions.orchestrator.generate_title", _no_title),
    ]
    started = [p.start() for p in patches]
    compose_mock = started[1]
    _seed_account("alpha")
    await init_db(db_url=db_url)
    cli = FakeCli()
    spawns = Spawns(cli)
    orch = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                               usage_fn=_fake_usage, repo_root=str(repo_root))
    src = await orch.create_session("Source", cwd=str(clone), model="sonnet",
                                    permission_mode="full-auto")
    src.engine_session_id = SDK_SID
    orch._drivers[src.id]._engine_session_id = SDK_SID  # type: ignore[attr-defined]
    for tid, u in LIVE.items():
        src.record_prompt_uuid(tid, u)
    for name, (text, mode) in NOW.items():
        Path(src.cwd, name).write_text(text)
        os.chmod(Path(src.cwd, name), mode)
    transcript = _write_transcript(src.cwd)
    cli.cwd, cli.transcript = src.cwd, transcript
    try:
        yield Env(orch, spawns, cli, src, transcript, compose_mock, tmp_path)
    finally:
        await orch.stop()
        await dispose_db()
        for p in reversed(patches):
            p.stop()


@pytest.fixture
def emitted(monkeypatch):
    """Every event any Claude driver emits out of band (``driver.emit``)."""
    seen: list = []
    real = ClaudeAgentDriver.emit

    def spy(self, e):
        seen.append(e)
        real(self, e)
    monkeypatch.setattr(ClaudeAgentDriver, "emit", spy)
    return seen


async def _rewind(orch, sid: str, *, to: str = "live-2", mode: str = rw.MODE_FILES_AND_CONVERSATION,
                  **kw):
    kw.setdefault("request_id", str(uuid.uuid4()))
    return await orch.rewind_session(sid, to_turn_id=to, mode=mode, **kw)


async def _rows(sid: str) -> list[SessionRewind]:
    async with db_session() as db:
        return list((await db.execute(
            select(SessionRewind).where(SessionRewind.session_id == sid)
            .order_by(SessionRewind.created_at))).scalars())


def _backups(sid: str) -> list[str]:
    from viberpit import paths
    d = paths.rewind_dir() / sid
    return sorted(os.listdir(d)) if d.is_dir() else []


# --- dry run ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_lists_worktree_relative_files_and_deletions_and_changes_nothing(
        env: Env) -> None:
    before_files, before_jsonl = _state(env.cwd), env.transcript.read_bytes()
    out = await _rewind(env.orch, env.src.id, dry_run=True)
    assert out == {"files": ["a.txt", "new.txt", "gone.txt"], "deleted": ["new.txt"],
                   "insertions": 1, "deletions": 2, "checkpointAvailable": True}
    assert env.cli.sent == [{"subtype": "rewind_files", "user_message_id": U2,
                             "dry_run": True}]
    assert _state(env.cwd) == before_files
    assert env.transcript.read_bytes() == before_jsonl
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []


@pytest.mark.asyncio
async def test_dry_run_without_a_checkpoint_offers_conversation_only(env: Env) -> None:
    env.cli.override["dry"] = lambda r: {"canRewind": False,
                                         "error": "No file checkpoint found for this message."}
    out = await _rewind(env.orch, env.src.id, dry_run=True)
    assert out == {"files": [], "deleted": [], "insertions": 0, "deletions": 0,
                   "checkpointAvailable": False}


# --- real rewind --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rewind_runs_in_order_records_emits_and_returns_the_prefill(
        env: Env, emitted, monkeypatch) -> None:
    before_len = env.transcript.stat().st_size
    real_backup = rw.take_backup

    def backup(*a, **k):
        env.cli.log.append("backup")
        return real_backup(*a, **k)
    monkeypatch.setattr(rw, "take_backup", backup)

    out = await _rewind(env.orch, env.src.id, to="live-2")

    assert env.cli.kinds() == ["dry", "backup", "files", "conv"]
    assert env.cli.sent[1:] == [
        {"subtype": "rewind_files", "user_message_id": U2},
        {"subtype": "rewind_conversation", "target_message_uuid": U2,
         "last_seen_user_message_uuid": U3}]
    assert out["prefill"] == PROMPTS[U2]
    assert out["filesRestored"] == ["a.txt", "new.txt", "gone.txt"]
    rows = await _rows(env.src.id)
    assert [r.id for r in rows] == [out["rewindId"]]
    row = rows[0]
    assert (row.to_prompt_uuid, row.to_turn_id, row.jsonl_path) == (
        U2, "live-2", str(env.transcript))
    # The length is recorded after the file restore, just before the cut.
    assert row.jsonl_len == env.cli.conv_saw_len == before_len
    assert row.files_restored == out["filesRestored"]
    assert row.created_at is not None and row.expired_at is None and row.undone_at is None
    assert row.backup_dir and Path(str(row.backup_dir)).is_dir()
    assert [e.to_wire() for e in emitted if isinstance(e, ev.Rewound)] == [
        {"type": "rewound", "rewindId": out["rewindId"], "toTurnId": "live-2",
         "filesRestored": ["a.txt", "new.txt", "gone.txt"]}]
    # The cut prompts are forgotten: the next last_seen is U1.
    assert list(env.src.prompt_uuids.values()) == [U1]


@pytest.mark.asyncio
async def test_a_replayed_user_turn_id_is_its_own_prompt_uuid(env: Env) -> None:
    env.src.prompt_uuids.clear()
    out = await _rewind(env.orch, env.src.id, to=U2, mode=rw.MODE_CONVERSATION)
    assert env.cli.sent[-1]["target_message_uuid"] == U2
    assert out["prefill"] == PROMPTS[U2]


@pytest.mark.asyncio
async def test_conversation_only_mode_never_touches_the_files(env: Env) -> None:
    before = _state(env.cwd)
    out = await _rewind(env.orch, env.src.id, mode=rw.MODE_CONVERSATION)
    assert env.cli.kinds() == ["conv"]
    assert out["filesRestored"] == [] and _state(env.cwd) == before
    (row,) = await _rows(env.src.id)
    assert row.backup_dir is None


@pytest.mark.asyncio
async def test_files_mode_without_a_checkpoint_is_a_coded_refusal(env: Env) -> None:
    env.cli.override["dry"] = lambda r: {"canRewind": False,
                                         "error": "No file checkpoint found for this message."}
    before, jsonl = _state(env.cwd), env.transcript.read_bytes()
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id)
    assert ei.value.code == rw.REWIND_NO_CHECKPOINT
    assert env.cli.kinds() == ["dry"]
    assert _state(env.cwd) == before and env.transcript.read_bytes() == jsonl
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []


@pytest.mark.parametrize("busy", ["turn", "permission"])
@pytest.mark.asyncio
async def test_busy_session_is_refused(env: Env, busy: str) -> None:
    driver = env.orch._drivers[env.src.id]
    if busy == "turn":
        driver._turn_active = True  # type: ignore[attr-defined]
    else:
        driver._reg.pending = lambda sid: [{"permId": "p"}]  # type: ignore[attr-defined]
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id)
    assert ei.value.code == rw.REWIND_BUSY
    assert env.cli.sent == []


@pytest.mark.asyncio
async def test_targets_that_are_not_user_prompts_are_refused(env: Env) -> None:
    env.src.record_turn_anchor("assistant-turn", A2)
    with pytest.raises(rw.RewindError) as not_user:
        await _rewind(env.orch, env.src.id, to="assistant-turn")
    with pytest.raises(rw.RewindError) as unknown:
        await _rewind(env.orch, env.src.id, to="no-such-turn")
    assert not_user.value.code == rw.REWIND_NOT_USER_MESSAGE
    assert unknown.value.code == rw.REWIND_TARGET_NOT_FOUND
    assert env.cli.sent == []


@pytest.mark.parametrize("why", ["codex", "kill_switch", "assistant"])
@pytest.mark.asyncio
async def test_rewind_is_unavailable_without_the_capability(env: Env, why: str) -> None:
    if why == "codex":
        env.src.engine = AgentEngine.CODEX
    elif why == "assistant":
        env.src.kind = "assistant"
    with patch.dict(os.environ, {"VIBERPIT_REWIND_ENABLED": "false"} if why == "kill_switch"
                    else {}):
        kws: list[dict[str, Any]] = [{"dry_run": True}, {}]
        for kw in kws:
            with pytest.raises(rw.RewindError) as ei:
                await _rewind(env.orch, env.src.id, **kw)
            assert ei.value.code == rw.REWIND_UNAVAILABLE
    assert env.cli.sent == []


@pytest.mark.asyncio
async def test_unknown_session_is_a_keyerror(env: Env) -> None:
    with pytest.raises(KeyError):
        await _rewind(env.orch, "nope")
    with pytest.raises(KeyError):
        await env.orch.undo_rewind("nope", "r")


# --- undo -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_truncates_restores_and_resumes(env: Env, emitted) -> None:
    before_files, before_jsonl = _state(env.cwd), env.transcript.read_bytes()
    out = await _rewind(env.orch, env.src.id)
    assert _state(env.cwd) != before_files and env.transcript.read_bytes() != before_jsonl
    (row,) = await _rows(env.src.id)
    spawns_before = len(env.spawns.calls)

    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK

    # Exactly the recorded length: the pre-rewind file, byte for byte.
    assert env.transcript.stat().st_size == row.jsonl_len
    assert env.transcript.read_bytes() == before_jsonl
    # Content, a mode-only revert (mode.sh), a file the rewind deleted (new.txt) and
    # one it re-created that was absent before it (gone.txt) are all back.
    assert _state(env.cwd) == before_files
    assert len(env.spawns.calls) == spawns_before + 1
    assert env.spawns.calls[-1]["resume"] == SDK_SID
    assert [e.to_wire() for e in emitted if isinstance(e, ev.RewindUndone)] == [
        {"type": "rewindUndone", "rewindId": out["rewindId"]}]
    assert not Path(str(row.backup_dir)).exists()
    (row,) = await _rows(env.src.id)
    assert row.undone_at is not None and row.expired_at is None


@pytest.mark.asyncio
async def test_undo_after_a_rotation_uses_the_recorded_transcript(env: Env) -> None:
    """The session moved to another account (another CLAUDE_CONFIG_DIR) since the
    rewind: undo still truncates the transcript it recorded and resumes there."""
    before_jsonl = env.transcript.read_bytes()
    out = await _rewind(env.orch, env.src.id)
    _seed_account("beta")
    beta_cfg = env.tmp / "cfg" / "beta"; beta_cfg.mkdir(parents=True)
    (beta_cfg / ".claude.json").write_text("{}")
    env.compose.return_value = beta_cfg
    env.src.account = "beta"
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    assert env.transcript.read_bytes() == before_jsonl
    assert env.spawns.calls[-1]["config_dir"] == str(beta_cfg)
    assert env.spawns.calls[-1]["resume"] == SDK_SID
    assert env.src.account == "beta"


@pytest.mark.asyncio
async def test_a_prompt_expires_the_rewind_and_drops_its_backup(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    (row,) = await _rows(env.src.id)
    assert await env.orch.send_prompt(env.src.id, "next") == "sent"
    (row,) = await _rows(env.src.id)
    assert row.expired_at is not None and not Path(str(row.backup_dir)).exists()
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED


@pytest.mark.asyncio
async def test_only_the_latest_rewind_is_undoable(env: Env) -> None:
    first = await _rewind(env.orch, env.src.id, to="live-3")
    second = await _rewind(env.orch, env.src.id, to="live-2")
    rows = {r.id: r for r in await _rows(env.src.id)}
    assert rows[first["rewindId"]].expired_at is not None
    assert not Path(str(rows[first["rewindId"]].backup_dir)).exists()
    assert rows[second["rewindId"]].expired_at is None
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, first["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    assert await env.orch.undo_rewind(env.src.id, second["rewindId"]) == OK


@pytest.mark.asyncio
async def test_a_turn_written_since_expires_the_rewind_after_a_restart(env: Env) -> None:
    """A daemon restart forgets the prompt hook's state; undo re-reads the transcript."""
    out = await _rewind(env.orch, env.src.id)
    env.orch.__dict__["_rewind_undoable"].clear()
    with env.transcript.open("a") as fh:
        fh.write(json.dumps(_user(U3, A1) | {"uuid": str(uuid.uuid4())}) + "\n")
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    (row,) = await _rows(env.src.id)
    assert row.expired_at is not None


@pytest.mark.asyncio
async def test_archive_drops_every_backup_and_expires_the_rewind(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    assert _backups(env.src.id) == [out["rewindId"]]
    await env.orch.archive_session(env.src.id)
    for _ in range(50):
        if not _backups(env.src.id):
            break
        await asyncio.sleep(0.02)
    assert _backups(env.src.id) == []
    (row,) = await _rows(env.src.id)
    assert row.expired_at is not None


# --- idempotency and races ----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_same_request_id_returns_the_same_rewind_once(env: Env) -> None:
    rid = str(uuid.uuid4())
    first, again = await asyncio.gather(_rewind(env.orch, env.src.id, request_id=rid),
                                        _rewind(env.orch, env.src.id, request_id=rid))
    assert first["rewindId"] == again["rewindId"] == rw.rewind_id_for(env.src.id, rid)
    assert env.cli.kinds().count("conv") == 1
    env.orch.__dict__["_rewind_results"].clear()        # e.g. after a daemon restart
    later = await _rewind(env.orch, env.src.id, request_id=rid)
    assert later == first                    # the prefill survives too (the row keeps it)
    assert env.cli.kinds().count("conv") == 1
    assert len(await _rows(env.src.id)) == 1


@pytest.mark.asyncio
async def test_concurrent_undos_apply_once(env: Env, emitted) -> None:
    out = await _rewind(env.orch, env.src.id)
    spawns_before = len(env.spawns.calls)
    a, b = await asyncio.gather(env.orch.undo_rewind(env.src.id, out["rewindId"]),
                                env.orch.undo_rewind(env.src.id, out["rewindId"]))
    assert a == b == OK
    assert len(env.spawns.calls) == spawns_before + 1
    assert len([e for e in emitted if isinstance(e, ev.RewindUndone)]) == 1


@pytest.mark.asyncio
async def test_undo_first_then_a_prompt_is_a_clean_undo(env: Env) -> None:
    before_jsonl = env.transcript.read_bytes()
    out = await _rewind(env.orch, env.src.id)
    undo = asyncio.ensure_future(env.orch.undo_rewind(env.src.id, out["rewindId"]))
    await asyncio.sleep(0)                      # the undo holds the session's lock
    sent = await env.orch.send_prompt(env.src.id, "after the undo")
    assert await undo == OK
    assert sent == "sent"
    # The prompt waited for the undo, then went to the RESUMED client.
    assert env.spawns.clients[-1].sent and env.spawns.clients[-2].sent == []
    assert env.transcript.read_bytes() == before_jsonl
    (row,) = await _rows(env.src.id)
    assert row.undone_at is not None and row.expired_at is None


@pytest.mark.asyncio
async def test_a_prompt_first_then_undo_is_expired_and_nothing_moves(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    prompt = asyncio.ensure_future(env.orch.send_prompt(env.src.id, "next"))
    await asyncio.sleep(0)
    undo = asyncio.ensure_future(env.orch.undo_rewind(env.src.id, out["rewindId"]))
    assert await prompt == "sent"
    with pytest.raises(rw.RewindError) as ei:
        await undo
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    assert _state(env.cwd)["a.txt"] == AT_U2["a.txt"]          # still rewound


# --- rollback -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_reply_to_rewind_files_restores_the_backup(env: Env) -> None:
    """The real rewind_files timed out after the CLI changed the files: they are put
    back from the backup and the rewind is refused with the outcome-unknown code."""
    before = _state(env.cwd)

    def applied_then_lost(request: dict) -> dict:
        env.cli.apply_files()
        raise TimeoutError("Control request timeout: rewind_files")
    env.cli.override["files"] = applied_then_lost
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id)
    assert ei.value.code == rw.REWIND_FAILED and ei.value.outcome_unknown
    assert ei.value.error_data == {"code": rw.REWIND_FAILED, "reason": rw.REWIND_OUTCOME_UNKNOWN}
    assert env.cli.kinds() == ["dry", "files"]
    assert _state(env.cwd) == before
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []


@pytest.mark.asyncio
async def test_no_reply_to_the_cut_restores_files_truncates_and_resumes(env: Env) -> None:
    before_files, before_jsonl = _state(env.cwd), env.transcript.read_bytes()
    spawns_before = len(env.spawns.calls)

    def cut_then_lost(request: dict) -> dict:
        env.cli.apply_conv(request["target_message_uuid"])
        raise TimeoutError("Control request timeout: rewind_conversation")
    env.cli.override["conv"] = cut_then_lost
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id)
    assert ei.value.outcome_unknown
    assert _state(env.cwd) == before_files
    assert env.transcript.read_bytes() == before_jsonl
    assert len(env.spawns.calls) == spawns_before + 1
    assert env.spawns.calls[-1]["resume"] == SDK_SID
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []


@pytest.mark.asyncio
async def test_a_refused_cut_after_the_file_restore_puts_the_files_back(env: Env) -> None:
    before_files, before_jsonl = _state(env.cwd), env.transcript.read_bytes()
    spawns_before = len(env.spawns.calls)
    env.cli.override["conv"] = lambda r: {"rewound": False, "reason": "stale_target",
                                          "error": "later turns exist"}
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id)
    assert ei.value.code == rw.REWIND_STALE_TARGET
    assert _state(env.cwd) == before_files and env.transcript.read_bytes() == before_jsonl
    assert len(env.spawns.calls) == spawns_before           # no respawn needed
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []


# --- backup store -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_backup_over_the_cap_is_refused_before_anything_changes(env: Env) -> None:
    big = Path(env.cwd, "a.txt")
    with big.open("r+b") as fh:
        fh.truncate(rw.REWIND_BACKUP_MAX_BYTES + 1)            # sparse: no real I/O
    before, jsonl = _state(env.cwd), env.transcript.read_bytes()
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id)
    assert ei.value.code == rw.REWIND_TOO_LARGE
    assert env.cli.kinds() == ["dry"]
    assert _state(env.cwd) == before and env.transcript.read_bytes() == jsonl
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []


@pytest.mark.asyncio
async def test_symlinks_in_the_backup_set_are_never_followed(env: Env) -> None:
    outside = env.tmp / "outside"; outside.mkdir()
    secret = outside / "secret.txt"; secret.write_text("SECRET\n")
    os.symlink(secret, Path(env.cwd, "link.txt"))
    os.symlink(outside, Path(env.cwd, "sub"))
    real_dry = env.cli._dry

    def dry(request: dict) -> dict:
        out = real_dry(request)
        out["filesChanged"] += [os.path.join(env.cwd, "link.txt"),
                                os.path.join(env.cwd, "sub", "secret.txt")]
        return out
    env.cli.override["dry"] = dry
    out = await _rewind(env.orch, env.src.id)
    (row,) = await _rows(env.src.id)
    manifest = json.loads(Path(str(row.backup_dir), "manifest.json").read_text())
    states = {e["rel"]: e["state"] for e in manifest["entries"]}
    assert states["link.txt"] == states["sub/secret.txt"] == rw.STATE_SKIPPED
    blobs = Path(str(row.backup_dir), "blobs")
    assert all(b"SECRET" not in p.read_bytes() for p in blobs.iterdir())
    secret.write_text("changed outside\n")
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == {
        "ok": True, "skipped": ["link.txt", "sub/secret.txt"]}
    assert secret.read_text() == "changed outside\n"
    assert os.path.islink(Path(env.cwd, "link.txt"))


def test_tracked_since_reads_the_spike_shape(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in _transcript_rows()) + "not json\n")
    got = rw.tracked_since(str(p), U2, "/w")
    assert got.paths == {"/w/a.txt", "/w/mode.sh", "/w/gone.txt", "/w/new.txt"}
    assert got.absent == {"/w/new.txt"}
    first = rw.tracked_since(str(p), U1, "/w")
    assert first.paths == {"/w/a.txt", "/w/mode.sh", "/w/gone.txt", "/w/new.txt"}
    assert rw.tracked_since(str(tmp_path / "missing"), U2, "/w").paths == frozenset()


def test_replace_file_beneath_refuses_a_symlinked_parent_and_replaces_a_symlink_leaf(
        tmp_path: Path) -> None:
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "f").write_text("keep")
    os.symlink(outside, root / "d")
    os.symlink(outside / "f", root / "leaf")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(fs_beneath.SymlinkInPath):
            fs_beneath.replace_file_beneath(fd, "d/f", b"x", 0o644)
        fs_beneath.replace_file_beneath(fd, "leaf", b"new", 0o600)
        assert not os.path.islink(root / "leaf") and (root / "leaf").read_bytes() == b"new"
        assert stat.S_IMODE((root / "leaf").stat().st_mode) == 0o600
        assert (outside / "f").read_text() == "keep"
        os.symlink(outside / "f", root / "leaf2")
        assert fs_beneath.unlink_file_beneath(fd, "leaf2") is False
        assert fs_beneath.unlink_file_beneath(fd, "missing/x") is False
        assert fs_beneath.unlink_file_beneath(fd, "leaf") is True
    finally:
        os.close(fd)
    assert (outside / "f").read_text() == "keep"


# --- fix round 1: undo never truncates a real turn or drops the only backup ----------

def _restart(orch) -> None:
    """What a daemon restart forgets: every in-memory rewind state."""
    for key in ("_rewind_undoable", "_rewind_locks", "_rewind_results", "_rewind_cut_prompts"):
        orch.__dict__.pop(key, None)


@pytest.mark.asyncio
async def test_a_restart_then_a_prompt_expires_the_rewind(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    _restart(env.orch)
    await env.orch._seed_undoable_rewinds()      # what rehydrate runs at boot
    assert await env.orch.send_prompt(env.src.id, "after the restart") == "sent"
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    (row,) = await _rows(env.src.id)
    assert row.expired_at is not None
    assert _state(env.cwd)["a.txt"] == AT_U2["a.txt"]          # still rewound


@pytest.mark.asyncio
async def test_rehydrate_seeds_the_undoable_rewind(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    _restart(env.orch)
    await env.orch.rehydrate()
    assert env.orch.__dict__["_rewind_undoable"] == {env.src.id: out["rewindId"]}


@pytest.mark.asyncio
async def test_a_turn_written_while_the_client_closes_is_never_truncated(env: Env) -> None:
    """The CLI starts a turn on its own while undo stops it: its rows land after the
    pre-check. The tail is re-checked once the client is down; nothing is truncated or
    restored, the session is resumed as it is, and the undo is refused."""
    out = await _rewind(env.orch, env.src.id)
    driver = env.orch._drivers[env.src.id]
    real_close = driver.aclose
    late = {"type": "user", "uuid": str(uuid.uuid4()), "parentUuid": A1,
            "message": {"role": "user", "content": "<task-notification>"}}

    async def close_with_a_late_turn(**kw: Any) -> None:
        with env.transcript.open("a") as fh:
            fh.write(json.dumps(late) + "\n")
        await real_close(**kw)
    driver.aclose = close_with_a_late_turn  # type: ignore[method-assign,assignment]
    rewound_files, jsonl = _state(env.cwd), env.transcript.read_bytes()
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    # Nothing truncated: the rewind's rows and the late turn are all still there.
    assert env.transcript.read_bytes() == jsonl + (json.dumps(late) + "\n").encode()
    assert _state(env.cwd) == rewound_files
    assert env.orch._drivers.get(env.src.id) is not None   # resumed, untouched
    assert env.spawns.calls[-1]["resume"] == SDK_SID


@pytest.mark.asyncio
async def test_a_torn_trailing_line_refuses_the_undo(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    with env.transcript.open("a") as fh:
        fh.write('{"type": "cost-st')
    jsonl = env.transcript.read_bytes()
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    assert env.transcript.read_bytes() == jsonl


def test_the_tail_check_allows_only_the_rewinds_own_rows(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text("x\n")
    base = p.stat().st_size
    rows = [{"type": "last-prompt", "leafUuid": A1, "explicit": True, "rewound": True,
             "cutUuid": U2, "sessionId": SDK_SID},
            {"type": "last-prompt", "lastPrompt": "p", "leafUuid": A1, "sessionId": SDK_SID},
            {"type": "cost-state", "totalCostUSD": 0.1}]
    with p.open("a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    assert rw.tail_is_rewind_only(str(p), base) is True
    for bad in ({"type": "system", "uuid": "x"}, {"type": "attachment"},
                {"type": "cost-state", "uuid": "x"}):
        q = tmp_path / f"{bad['type']}.jsonl"
        q.write_bytes(p.read_bytes() + (json.dumps(bad) + "\n").encode())
        assert rw.tail_is_rewind_only(str(q), base) is False, bad
    assert rw.tail_is_rewind_only(str(p), base + 10_000) is False     # shorter: rewritten


@pytest.mark.asyncio
async def test_undo_refuses_while_a_turn_is_starting(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    env.orch._drivers[env.src.id]._starting_turn = True  # type: ignore[attr-defined]
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_BUSY


@pytest.mark.parametrize("damage", ["no_manifest", "bad_manifest", "other_host"])
@pytest.mark.asyncio
async def test_undo_prechecks_the_backup_before_stopping_anything(env: Env, damage: str) -> None:
    out = await _rewind(env.orch, env.src.id)
    (row,) = await _rows(env.src.id)
    manifest = Path(str(row.backup_dir), "manifest.json")
    if damage == "no_manifest":
        manifest.unlink()
    elif damage == "bad_manifest":
        manifest.write_text("{not json")
    else:
        async with db_session() as db:
            row_ = await db.get(SessionRewind, out["rewindId"])
            assert row_ is not None
            row_.host = "some-other-host"
    spawns_before, jsonl = len(env.spawns.calls), env.transcript.read_bytes()
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_UNDO_EXPIRED
    assert len(env.spawns.calls) == spawns_before           # nothing was stopped
    assert env.transcript.read_bytes() == jsonl


@pytest.mark.asyncio
async def test_undo_restores_the_files_before_it_truncates(env: Env, monkeypatch) -> None:
    out = await _rewind(env.orch, env.src.id)
    order: list[str] = []
    real_restore, real_truncate = rw.restore_backup, rw.truncate_transcript

    def restore(*a, **k):
        order.append("restore")
        return real_restore(*a, **k)

    def truncate(*a, **k):
        order.append("truncate")
        return real_truncate(*a, **k)
    monkeypatch.setattr(rw, "restore_backup", restore)
    monkeypatch.setattr(rw, "truncate_transcript", truncate)
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    assert order == ["restore", "truncate"]


@pytest.mark.asyncio
async def test_a_failed_restore_keeps_the_backup_and_names_the_paths(
        env: Env, monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    out = await _rewind(env.orch, env.src.id)
    (row,) = await _rows(env.src.id)
    real = rw.replace_file_beneath

    def eacces(root_fd, rel, data, mode):
        if rel == "a.txt":
            raise PermissionError(13, "Permission denied", rel)
        return real(root_fd, rel, data, mode)
    monkeypatch.setattr(rw, "replace_file_beneath", eacces)
    caplog.set_level(logging.INFO)
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_FAILED
    assert ei.value.error_data["failed"] == ["a.txt"]
    assert Path(str(row.backup_dir), "manifest.json").is_file()     # kept
    (row,) = await _rows(env.src.id)
    assert row.undone_at is None
    assert any(r.levelno == logging.WARNING and "restore" in r.getMessage()
               for r in caplog.records)
    # The same undo retried once the path is writable again finishes the job.
    monkeypatch.setattr(rw, "replace_file_beneath", real)
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    assert _state(env.cwd)["a.txt"] == NOW["a.txt"]
    assert not Path(str(row.backup_dir)).exists()


@pytest.mark.asyncio
async def test_a_rollback_whose_reset_returns_early_still_restores(env: Env, monkeypatch) -> None:
    before = _state(env.cwd)

    def applied_then_lost(request: dict) -> dict:
        env.cli.apply_files()
        raise TimeoutError("Control request timeout: rewind_files")
    env.cli.override["files"] = applied_then_lost

    async def no_swap(*a, **k):
        return "failed"                       # e.g. a prior swap still in flight
    monkeypatch.setattr(env.orch, "_reassign", no_swap)
    with pytest.raises(rw.RewindError):
        await _rewind(env.orch, env.src.id)
    assert _state(env.cwd) == before
    assert _backups(env.src.id) == []


@pytest.mark.asyncio
async def test_no_reply_to_rewind_files_stops_the_client_before_restoring(
        env: Env, monkeypatch) -> None:
    spawns_before = len(env.spawns.calls)
    inside_swap: list[bool] = []
    real = rw.restore_backup

    def restore(*a, **k):
        inside_swap.append(env.src.id in env.orch._swapping)
        return real(*a, **k)
    monkeypatch.setattr(rw, "restore_backup", restore)

    def applied_then_lost(request: dict) -> dict:
        env.cli.apply_files()
        raise TimeoutError("Control request timeout: rewind_files")
    env.cli.override["files"] = applied_then_lost
    jsonl = env.transcript.read_bytes()
    with pytest.raises(rw.RewindError):
        await _rewind(env.orch, env.src.id)
    assert inside_swap == [True]               # the old client was already stopped
    assert len(env.spawns.calls) == spawns_before + 1
    assert env.transcript.read_bytes() == jsonl  # nothing to truncate


@pytest.mark.asyncio
async def test_last_seen_is_the_newest_prompt_again_after_an_undo(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id, to="live-2")
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    env.cli.sent.clear()
    again = await _rewind(env.orch, env.src.id, to="live-1")
    conv = [s for s in env.cli.sent if s["subtype"] == "rewind_conversation"]
    assert conv[-1]["last_seen_user_message_uuid"] == U3
    assert again["prefill"] == PROMPTS[U1]


@pytest.mark.asyncio
async def test_a_rotation_after_a_rewind_replays_no_cut_prompt(env: Env) -> None:
    env.orch._last_prompt[env.src.id] = PROMPTS[U3]
    await _rewind(env.orch, env.src.id)
    assert await env.orch._reassign(env.src, env.src.account, reason="manual_rotate") == "rotated"
    assert env.spawns.clients[-1].sent == []


@pytest.mark.asyncio
async def test_a_prompt_waits_a_bounded_time_for_a_rewind_in_flight(
        env: Env, monkeypatch) -> None:
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_SWAP_PROMPT_HOLD_S", 0.05)
    lock = env.orch._rewind_lock(env.src.id)
    await lock.acquire()                       # a rewind that never finishes
    try:
        assert await env.orch.send_prompt(env.src.id, "hello") == "recovering"
    finally:
        lock.release()
    assert env.spawns.clients[-1].sent == []


@pytest.mark.asyncio
async def test_undo_publishes_no_rotation_and_keeps_a_pause(env: Env, emitted) -> None:
    out = await _rewind(env.orch, env.src.id)
    published: list[dict] = []
    real = env.orch._publish_event
    def spy(event: dict) -> None:
        published.append(event)
        real(event)
    env.orch._publish_event = spy  # type: ignore[method-assign,assignment]
    env.src.status, env.src.paused_reason = "limited", "usage_limit"
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    assert [e for e in published if e.get("type") == "rotated"] == []
    assert (env.src.status, env.src.paused_reason) == ("limited", "usage_limit")
    assert [e for e in emitted if isinstance(e, ev.RewindUndone)]


@pytest.mark.asyncio
async def test_an_expiry_failure_still_delivers_the_prompt(
        env: Env, caplog: pytest.LogCaptureFixture) -> None:
    out = await _rewind(env.orch, env.src.id)

    async def broken(*a, **k):
        raise RuntimeError("database is locked")
    env.orch._expire_rewinds = broken  # type: ignore[method-assign]
    caplog.set_level(logging.INFO)
    assert await env.orch.send_prompt(env.src.id, "next") == "sent"
    assert env.orch.__dict__["_rewind_undoable"] == {env.src.id: out["rewindId"]}
    assert any(r.levelno == logging.WARNING and "expire" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_archive_waits_for_an_undo_in_flight(env: Env) -> None:
    out = await _rewind(env.orch, env.src.id)
    lock = env.orch._rewind_lock(env.src.id)
    await lock.acquire()
    try:
        task = asyncio.ensure_future(env.orch._drop_session_rewinds(env.src.id))
        await asyncio.sleep(0.05)
        assert not task.done() and _backups(env.src.id) == [out["rewindId"]]
    finally:
        lock.release()
    await task
    assert _backups(env.src.id) == []


@pytest.mark.asyncio
async def test_undo_reports_skipped_paths(env: Env) -> None:
    outside = env.tmp / "outside"; outside.mkdir()
    (outside / "s.txt").write_text("s\n")
    os.symlink(outside / "s.txt", Path(env.cwd, "link.txt"))
    real_dry = env.cli._dry
    env.cli.override["dry"] = lambda r: {**real_dry(r), "filesChanged": [
        *real_dry(r)["filesChanged"], os.path.join(env.cwd, "link.txt")]}
    out = await _rewind(env.orch, env.src.id)
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == {
        "ok": True, "skipped": ["link.txt"]}


def test_a_mode_000_file_is_restored_as_mode_000(tmp_path: Path) -> None:
    """A recorded mode of 0 is a real mode, not "missing" (a non-root user cannot
    read a mode-000 file to back it up, so the manifest records it directly)."""
    root = tmp_path / "w"; root.mkdir()
    f = root / "locked"; f.write_text("x")
    dest = tmp_path / "b"
    rw.take_backup(dest, str(root), [str(f)])
    manifest = json.loads((dest / "manifest.json").read_text())
    manifest["entries"][0]["mode"] = 0
    (dest / "manifest.json").write_text(json.dumps(manifest))
    f.write_text("changed")
    rw.restore_backup(dest)
    try:
        assert stat.S_IMODE(f.stat().st_mode) == 0
    finally:
        os.chmod(f, 0o644)


# --- fix round 2: partial failures report truthfully ---------------------------------

@pytest.mark.asyncio
async def test_a_failed_truncation_never_marks_the_undo_done(env: Env, monkeypatch) -> None:
    out = await _rewind(env.orch, env.src.id, to="live-2")
    rewound_len = env.transcript.stat().st_size
    real = rw.truncate_transcript

    def eperm(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(rw, "truncate_transcript", eperm)
    with pytest.raises(rw.RewindError) as ei:
        await env.orch.undo_rewind(env.src.id, out["rewindId"])
    assert ei.value.code == rw.REWIND_FAILED
    (row,) = await _rows(env.src.id)
    assert row.undone_at is None and row.expired_at is None
    assert Path(str(row.backup_dir), "manifest.json").is_file()
    assert env.transcript.stat().st_size == rewound_len
    assert env.orch._drivers.get(env.src.id) is not None      # resumed, not parked
    # A retry once the transcript is writable finishes the undo.
    monkeypatch.setattr(rw, "truncate_transcript", real)
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    assert env.transcript.stat().st_size == row.jsonl_len


def _no_swap(env: Env, monkeypatch) -> None:
    async def failed(*a, **k):
        return "failed"                        # e.g. a prior swap still in flight
    monkeypatch.setattr(env.orch, "_reassign", failed)


@pytest.mark.asyncio
async def test_an_applied_cut_with_no_reply_and_no_swap_is_kept_and_undoable(
        env: Env, monkeypatch, emitted) -> None:
    """The cut's reply never came and the client could not be stopped, but the
    transcript shows the CLI applied it: the rewind is recorded as done (files stay
    rewound, backup kept), so the state is consistent and can be undone."""
    def cut_then_lost(request: dict) -> dict:
        env.cli.apply_conv(request["target_message_uuid"])
        raise TimeoutError("Control request timeout: rewind_conversation")
    env.cli.override["conv"] = cut_then_lost
    real_reassign = env.orch._reassign
    _no_swap(env, monkeypatch)
    rid = str(uuid.uuid4())
    with pytest.raises(rw.RewindError) as ei:
        await _rewind(env.orch, env.src.id, request_id=rid)
    assert ei.value.outcome_unknown
    rewind_id = rw.rewind_id_for(env.src.id, rid)
    (row,) = await _rows(env.src.id)
    assert row.id == rewind_id and row.expired_at is None
    assert _state(env.cwd)["a.txt"] == AT_U2["a.txt"]          # files stay rewound
    assert Path(str(row.backup_dir), "manifest.json").is_file()
    assert [e.rewind_id for e in emitted if isinstance(e, ev.Rewound)] == [rewind_id]
    assert env.orch.__dict__["_rewind_undoable"] == {env.src.id: rewind_id}
    monkeypatch.setattr(env.orch, "_reassign", real_reassign)
    assert await env.orch.undo_rewind(env.src.id, rewind_id) == OK
    assert _state(env.cwd)["a.txt"] == NOW["a.txt"]


@pytest.mark.asyncio
async def test_an_unapplied_cut_with_no_reply_and_no_swap_restores_and_parks(
        env: Env, monkeypatch) -> None:
    before = _state(env.cwd)

    def lost(request: dict) -> dict:
        raise TimeoutError("Control request timeout: rewind_conversation")
    env.cli.override["conv"] = lost
    _no_swap(env, monkeypatch)
    with pytest.raises(rw.RewindError):
        await _rewind(env.orch, env.src.id)
    assert _state(env.cwd) == before
    assert await _rows(env.src.id) == [] and _backups(env.src.id) == []
    assert (env.src.status, env.src.paused_reason) == ("needs_attention", "reassign_failed")


#: What CLI 2.1.317 appends when a resumed, idle client exits (probe 2026-11-03,
#: verification doc "Third probe"): no uuid, not a conversation entry.
_EXIT_ROWS: list[dict[str, Any]] = [{"type": "tools-used", "tools": ["Read", "Edit"], "sessionId": SDK_SID},
                                    {"type": "cost-state", "sessionId": SDK_SID, "totalCostUSD": 0.0}]


@pytest.mark.asyncio
async def test_the_rows_a_closing_cli_appends_do_not_block_the_undo(env: Env) -> None:
    before_jsonl = env.transcript.read_bytes()
    out = await _rewind(env.orch, env.src.id)
    driver = env.orch._drivers[env.src.id]
    real_close = driver.aclose

    async def close_like_the_cli(**kw: Any) -> None:
        with env.transcript.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in _EXIT_ROWS))
        await real_close(**kw)
    driver.aclose = close_like_the_cli  # type: ignore[method-assign,assignment]
    assert await env.orch.undo_rewind(env.src.id, out["rewindId"]) == OK
    assert env.transcript.read_bytes() == before_jsonl


def test_the_tail_check_allows_the_exit_rows(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text("x\n" + "".join(json.dumps(r) + "\n" for r in _EXIT_ROWS))
    assert rw.tail_is_rewind_only(str(p), 2) is True
    q = tmp_path / "u.jsonl"
    q.write_text("x\n" + json.dumps({**_EXIT_ROWS[0], "uuid": "u"}) + "\n")
    assert rw.tail_is_rewind_only(str(q), 2) is False


# --- RPC / remote / fleet / facade --------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_round_trip_runs_on_the_owner_daemon_and_keeps_codes(
        env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    app = create_daemon_app()
    register_orchestrator_rpc(app, env.orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            preview = await _rewind(ro, env.src.id, dry_run=True)
            out = await _rewind(ro, env.src.id, identity="u@x")
            with pytest.raises(rw.RewindError) as not_found:
                await _rewind(ro, env.src.id, to="no-such-turn")
            env.cli.override["conv"] = lambda r: (_ for _ in ()).throw(
                TimeoutError("no reply"))
            with pytest.raises(rw.RewindError) as unknown:
                await _rewind(ro, env.src.id, to="live-1")
            with pytest.raises(KeyError):
                await _rewind(ro, "nope")
            assert await ro.undo_rewind(env.src.id, out["rewindId"]) == OK
            assert await ro.undo_rewind(env.src.id, out["rewindId"]) == OK
        finally:
            await client.aclose()
    assert preview["checkpointAvailable"] is True
    assert out["prefill"] == PROMPTS[U2]
    assert not_found.value.code == rw.REWIND_TARGET_NOT_FOUND
    assert unknown.value.code == rw.REWIND_FAILED
    assert unknown.value.reason == rw.REWIND_OUTCOME_UNKNOWN
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [(r.name, r.getMessage()) for r in warnings]


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs, timeout))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_kwargs_with_the_rewind_budget() -> None:
    client = _RecordingClient(result={"ok": True})
    ro = RemoteOrchestrator(client)
    await ro.rewind_session("s", to_turn_id="t", mode="conversation", request_id="r")
    await ro.undo_rewind("s", "rw")
    assert client.calls == [
        ("rewind_session", {"session_id": "s", "to_turn_id": "t", "mode": "conversation",
                            "request_id": "r", "dry_run": False, "identity": "system"},
         REWIND_RPC_TIMEOUT),
        ("undo_rewind", {"session_id": "s", "rewind_id": "rw", "identity": "system"},
         REWIND_RPC_TIMEOUT)]
    # Above a resume spawn, three control requests and a rollback swap.
    assert REWIND_RPC_TIMEOUT > (SPAWN_RPC_TIMEOUT + 3 * rw.REWIND_CONTROL_TIMEOUT_S
                                 + DRIVER_SWAP_TIMEOUT_S)


@pytest.mark.asyncio
async def test_remote_rebuilds_known_codes_and_never_invents_one() -> None:
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError(
        "RewindError", "m", {"code": rw.REWIND_BUSY})))
    with pytest.raises(rw.RewindError) as ei:
        await ro.undo_rewind("s", "r")
    assert ei.value.code == rw.REWIND_BUSY
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError("RewindError", "m",
                                                               {"code": "WAT"})))
    with pytest.raises(DaemonError):
        await _rewind(ro, "s")
    ro = RemoteOrchestrator(_RecordingClient(error=DaemonError("UnknownMethod",
                                                               "rewind_session")))
    with pytest.raises(DaemonError) as old:
        await _rewind(ro, "s")
    assert old.value.err_type == "UnknownMethod"


@pytest.mark.asyncio
async def test_fleet_routes_both_calls_to_the_owning_host() -> None:
    assert {"rewind_session", "undo_rewind"} <= _PER_SESSION

    class _Host:
        def __init__(self):
            self.calls: list = []

        async def rewind_session(self, session_id, **kw):
            self.calls.append(("rewind", session_id, kw))
            return {"rewindId": "r", "prefill": None, "filesRestored": []}

        async def undo_rewind(self, session_id, **kw):
            self.calls.append(("undo", session_id, kw))
            return {"ok": True}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _Host(), _Host()
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache["s"] = "rocky"
    await fo.rewind_session("s", to_turn_id="t", mode="conversation", request_id="q")
    await fo.undo_rewind("s", rewind_id="r")
    assert [c[0] for c in rocky.calls] == ["rewind", "undo"] and dobby.calls == []


# --- HTTP -------------------------------------------------------------------------

_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}
SID = "44444444-4444-4444-8444-444444444444"


def _client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=Spawns(FakeCli()), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = SessionRecord(id=SID, name="n", account="a",
                                       engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                                       cols=80, rows=24, cwd=None)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


def _post(c, body, sid=SID, path="rewind"):
    if path == "rewind":
        body = {"requestId": str(uuid.uuid4()), **body}
    return c.post(f"/api/sessions/{sid}/{path}", json=body, headers=_H)


def test_http_rewind_and_dry_run_are_200() -> None:
    rid = str(uuid.uuid4())

    async def fake(self, session_id, *, to_turn_id, mode, request_id, dry_run=False,
                   identity="system"):
        assert (session_id, to_turn_id, request_id, identity) == (
            SID, "t1", rid, "testuser@example.com")
        if dry_run:
            return {"files": ["a.txt"], "deleted": [], "insertions": 1, "deletions": 0,
                    "checkpointAvailable": True}
        return {"rewindId": "r1", "prefill": "hi", "filesRestored": ["a.txt"]}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "rewind_session", fake):
        dry = _post(c, {"toTurnId": "t1", "mode": "files_and_conversation", "dryRun": True,
                        "requestId": rid})
        real = _post(c, {"toTurnId": "t1", "mode": "conversation", "requestId": rid})
    assert dry.status_code == 200 and dry.json()["checkpointAvailable"] is True
    assert real.status_code == 200
    assert real.json() == {"rewindId": "r1", "prefill": "hi", "filesRestored": ["a.txt"]}


def test_http_undo_is_200() -> None:
    async def fake(self, session_id, rewind_id, identity="system"):
        assert (session_id, rewind_id) == (SID, "r1")
        return {"ok": True, "skipped": ["link.txt"]}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "undo_rewind", fake):
        r = _post(c, {"rewindId": "r1"}, path="rewind/undo")
    assert r.status_code == 200 and r.json() == {"ok": True, "skipped": ["link.txt"]}


def test_http_unknown_session_404() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"toTurnId": "t", "mode": "conversation"}, sid="nope").status_code == 404
        assert _post(c, {"rewindId": "r"}, sid="nope", path="rewind/undo").status_code == 404


def test_http_bad_body_422() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"toTurnId": "t", "mode": "files"}).status_code == 422
        assert c.post(f"/api/sessions/{SID}/rewind", headers=_H,
                      json={"toTurnId": "t", "mode": "conversation"}).status_code == 422


@pytest.mark.parametrize("code", sorted(rw.REWIND_CODES))
def test_http_refusals_are_409_with_their_code(code: str) -> None:
    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "rewind_session",
                         side_effect=rw.RewindError(code, "why")), \
            patch.object(SessionOrchestrator, "undo_rewind",
                         side_effect=rw.RewindError(code, "why")):
        r = _post(c, {"toTurnId": "t", "mode": "conversation"})
        u = _post(c, {"rewindId": "r"}, path="rewind/undo")
    for resp in (r, u):
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == code


def test_http_kill_switch_is_409_unavailable() -> None:
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_REWIND_ENABLED": "false"}), _client() as c:
        r = _post(c, {"toTurnId": "t", "mode": "conversation", "dryRun": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == rw.REWIND_UNAVAILABLE


def test_http_old_daemon_is_daemon_outdated_409() -> None:
    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "rewind_session",
                         side_effect=DaemonError("UnknownMethod", "rewind_session")), \
            patch.object(SessionOrchestrator, "undo_rewind",
                         side_effect=DaemonError("UnknownMethod", "undo_rewind")):
        r = _post(c, {"toTurnId": "t", "mode": "conversation"})
        u = _post(c, {"rewindId": "r"}, path="rewind/undo")
    for resp in (r, u):
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "daemon_outdated"


def test_http_requires_identity() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert c.post(f"/api/sessions/{SID}/rewind",
                      json={"toTurnId": "t", "mode": "conversation",
                            "requestId": str(uuid.uuid4())}).status_code == 401
