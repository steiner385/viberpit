"""D-13 memory files, server side: the per-session loaded set, the owning-host viewer
and the user-memory editor.

- The loaded set is the CLI's own ``get_context_usage().memoryFiles``: carried on the
  ``contextUsage`` event in CLI order, kept on the record (and its row), and re-emitted
  on attach.
- ``GET /api/sessions/{id}/memory-file?path=`` reads one file of that set on the daemon
  that owns the session: an exact member of the set only, resolved once, inside the
  worktree or one of the user files, opened with no symlink followed after resolving.
- ``GET``/``PUT /api/settings/memory`` edit the shared ``~/.claude/CLAUDE.md`` on the
  edge: sha256 etag, atomic same-directory write, a symlink refused.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import events as ev
from server.sessions import memory_files
from server.sessions.engine import AgentEngine
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.memory_files import (
    MAX_MEMORY_BYTES,
    MEMORY_FILE_UNKNOWN,
    MemoryFileError,
    normalize_memory_files,
)
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import WORKTREE_RPC_TIMEOUT, RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.ipc_harness import serve_daemon_uds
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401

SID = "33333333-3333-4333-8333-333333333333"
_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
}
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, **_GIT_ENV, "HOME": str(cwd)}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def _no_warnings(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------- the loaded set

_CLI_ROWS = [
    {"path": "/cfg/CLAUDE.md", "type": "User", "tokens": 36},
    {"path": "/home/u/homeimport.md", "type": "User", "tokens": 7},
    {"path": "/w/CLAUDE.md", "type": "Project", "tokens": 24},
    {"path": "/w/.claude/rules/r.md", "type": "Project", "tokens": 4},
    {"path": "/w/CLAUDE.local.md", "type": "Local", "tokens": 4},
]


def test_context_usage_wire_carries_memory_files_in_order() -> None:
    e = ev.ContextUsage(used_tokens=10, max_tokens=100, percentage=10.0,
                        memory_files=list(_CLI_ROWS))
    assert e.to_wire()["memoryFiles"] == _CLI_ROWS


def test_context_usage_wire_omits_memory_files_when_not_reported() -> None:
    """An engine with no loaded set (Codex) sends the pre-D-13 frame unchanged."""
    assert "memoryFiles" not in ev.ContextUsage(used_tokens=1, max_tokens=2,
                                                percentage=50.0).to_wire()


def test_normalize_keeps_cli_order_and_values_and_drops_malformed_rows() -> None:
    raw = [_CLI_ROWS[0], "junk", {"path": 3, "type": "User", "tokens": 1},
           {"path": "/x", "type": None, "tokens": 1}, _CLI_ROWS[2],
           {"path": "/y", "type": "Managed", "extra": "dropped"}, _CLI_ROWS[1]]
    assert normalize_memory_files(raw) == [
        _CLI_ROWS[0], _CLI_ROWS[2], {"path": "/y", "type": "Managed", "tokens": 0},
        _CLI_ROWS[1]]
    assert normalize_memory_files(None) is None
    assert normalize_memory_files("nope") is None
    assert normalize_memory_files([]) == []


def test_driver_emits_and_reports_memory_files_in_cli_order() -> None:
    from claude_agent_sdk import TextBlock

    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    from server.tests.fakes import fake_sdk as f
    from server.tests.fakes.fake_sdk import FakeSDKClient

    cu = {"totalTokens": 600, "maxTokens": 200000, "percentage": 0.3,
          "model": "claude-opus-4-8", "memoryFiles": list(_CLI_ROWS)}
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"), f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]
    reported: list[tuple[str, list]] = []
    seen: list[ev.ContextUsage] = []

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool, context_usage=cu)

    async def on_mem(sid, files):
        reported.append((sid, files))

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                              sdk_factory=make, registry=PermissionRegistry(), identity="me",
                              on_memory_files=on_mem)
        await d.start()
        await d.send_prompt("hi")
        agen = d.events()
        while True:
            try:
                e = await asyncio.wait_for(agen.__anext__(), timeout=0.5)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            if isinstance(e, ev.ContextUsage):
                seen.append(e)
                break
        await d.aclose()

    asyncio.run(run())
    assert seen and seen[0].memory_files == _CLI_ROWS
    assert reported == [("s1", _CLI_ROWS)]


def _rec(cwd: str | None = None, **kw) -> SessionRecord:
    return SessionRecord(id=SID, name="n", account="acct", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c", cols=80, rows=24,
                         cwd=cwd, **kw)  # type: ignore[arg-type]


def test_record_round_trips_memory_files_over_the_wire() -> None:
    r = _rec("/w", memory_files=list(_CLI_ROWS))
    assert r.to_dict()["memoryFiles"] == _CLI_ROWS
    assert SessionRecord.from_dict(r.to_dict()) == r
    assert SessionRecord.from_dict(_rec("/w").to_dict()).memory_files is None


@pytest.mark.asyncio
async def test_persist_memory_files_updates_record_and_row(isolated_db) -> None:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Session as DbSession

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    async with db_session() as db:
        db.add(DbSession(id=SID, name="n", account_name="acct", engine_session_id="c",
                         cwd="/w", status="idle", name_is_auto=True,
                         permission_mode="interactive"))
        await db.commit()
    orch._records[SID] = _rec("/w")
    await orch._persist_memory_files(SID, list(_CLI_ROWS))
    assert orch._records[SID].memory_files == _CLI_ROWS
    async with db_session() as db:
        row = (await db.execute(select(DbSession).where(DbSession.id == SID))).scalars().one()
    assert row.memory_files == _CLI_ROWS

    # rehydrate restores the set from the row (a daemon restart keeps the viewer working)
    fresh = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                usage_fn=lambda name: None)
    await fresh.rehydrate()
    assert fresh._records[SID].memory_files == _CLI_ROWS


def test_attach_reemits_the_loaded_set(ws_env) -> None:  # noqa: F811
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-mem")
    rec = orch._records[session.id]
    rec.context_used_tokens, rec.context_max_tokens = 1000, 200_000
    rec.memory_files = list(_CLI_ROWS)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session.id}))
        events = _drain_events(ws, until_type="replayComplete")
    cu = next((e for e in events if e.get("type") == "contextUsage"), None)
    assert cu is not None, [e.get("type") for e in events]
    assert cu["memoryFiles"] == _CLI_ROWS
    client.portal.call(orch.close_session, session.id)


# ---------------------------------------------------------------- the viewer


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A git worktree, a shared base (``~/.claude``) holding the user CLAUDE.md, an
    account cfg dir whose CLAUDE.md links to it, and files outside every allowed root."""
    shared = tmp_path / "dot-claude"
    shared.mkdir()
    (shared / "CLAUDE.md").write_text("# user rules\n")
    store = tmp_path / "store"
    cfg = store / "cfg" / "acct"
    cfg.mkdir(parents=True)
    (cfg / "CLAUDE.md").symlink_to(shared / "CLAUDE.md")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.setenv("VIBERPIT_HOME", str(store))

    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    (work / "CLAUDE.md").write_text("# project\n")
    (work / "CLAUDE.local.md").write_text("# local\n")
    secret = tmp_path / "shadow"
    secret.write_text("root:x:0:0\n")
    (work / "evil.md").symlink_to(secret)
    outside = tmp_path / "homeimport.md"
    outside.write_text("imported\n")
    big = work / "big.md"
    big.write_bytes(b"a" * (MAX_MEMORY_BYTES + 100))
    loaded = [
        {"path": str(cfg / "CLAUDE.md"), "type": "User", "tokens": 3},
        # a PROJECT import outside every root (a User one gets the literal walk)
        {"path": str(outside), "type": "Project", "tokens": 1},
        {"path": str(work / "CLAUDE.md"), "type": "Project", "tokens": 2},
        {"path": str(work / "sub" / ".." / ".." / "shadow"), "type": "Project", "tokens": 1},
        {"path": str(work / "evil.md"), "type": "Project", "tokens": 1},
        {"path": str(big), "type": "Project", "tokens": 9},
        {"path": str(work / "CLAUDE.local.md"), "type": "Local", "tokens": 1},
    ]
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    orch._records[SID] = _rec(str(work / "sub"), memory_files=loaded)
    return SimpleNamespace(tmp=tmp_path, shared=shared, cfg=cfg, work=work, secret=secret,
                           outside=outside, big=big, loaded=loaded, orch=orch)


@pytest.mark.asyncio
async def test_viewer_reads_a_set_file_inside_the_worktree(world) -> None:
    # The cwd is work/sub: the repo-root CLAUDE.md the CLI walked up to is still inside.
    out = await world.orch.memory_file(SID, path=str(world.work / "CLAUDE.md"))
    assert out == {"path": str(world.work / "CLAUDE.md"), "type": "Project",
                   "text": "# project\n", "truncated": False, "editable": False}


@pytest.mark.asyncio
async def test_viewer_reads_the_user_file_through_its_link_and_marks_it_editable(world) -> None:
    out = await world.orch.memory_file(SID, path=str(world.cfg / "CLAUDE.md"))
    assert out["text"] == "# user rules\n"
    assert out["type"] == "User" and out["editable"] is True
    assert out["path"] == str(world.cfg / "CLAUDE.md")  # the CLI's path, not the target


@pytest.mark.asyncio
async def test_a_private_account_claude_md_is_viewable_but_not_editable(world) -> None:
    """Task 1 keeps a real per-account CLAUDE.md private; the editor writes only the
    shared file, so that one is read-only here."""
    (world.cfg / "CLAUDE.md").unlink()
    (world.cfg / "CLAUDE.md").write_text("private\n")
    out = await world.orch.memory_file(SID, path=str(world.cfg / "CLAUDE.md"))
    assert out["text"] == "private\n" and out["editable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/etc/passwd",                         # not in the set
    "CLAUDE.md",                           # relative spelling of a set member
    "{work}/sub/../CLAUDE.md",             # traversal spelling of a set member
    "{work}/CLAUDE.md/",                   # trailing slash
    "{work}/notes.md",                     # inside the worktree, not loaded
])
async def test_viewer_refuses_paths_not_exactly_in_the_set(world, path: str) -> None:
    with pytest.raises(MemoryFileError) as ei:
        await world.orch.memory_file(SID, path=path.format(work=world.work))
    assert ei.value.code == MEMORY_FILE_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["outside", "traversal", "evil"])
async def test_viewer_refuses_set_entries_resolving_outside_the_allowed_roots(
        world, key: str, caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 2: a set entry that is a symlink to ``/etc/shadow`` (here a stand-in),
    a ``..`` spelling that walks out, and an out-of-tree import are all refused — at
    INFO, never WARNING."""
    caplog.set_level(logging.INFO)
    path = {"outside": world.outside,
            "traversal": world.work / "sub" / ".." / ".." / "shadow",
            "evil": world.work / "evil.md"}[key]
    with pytest.raises(MemoryFileError) as ei:
        await world.orch.memory_file(SID, path=str(path))
    assert ei.value.code == MEMORY_FILE_UNKNOWN
    assert ei.value.error_data == {"code": MEMORY_FILE_UNKNOWN}
    assert _no_warnings(caplog) == []


def test_a_component_swapped_for_a_symlink_after_resolving_is_not_followed(world) -> None:
    """The open walks the resolved path with O_NOFOLLOW: a directory swapped for an
    outside link between the realpath and the open is refused, not followed."""
    real = os.path.realpath
    target = str(world.work / "CLAUDE.md")

    decoy = world.tmp / "decoy"
    decoy.mkdir()
    (decoy / "CLAUDE.md").write_text("decoy\n")
    cwd = str(world.work / "sub")

    def racing_realpath(p, *a, **k):
        out = real(p, *a, **k)
        if p == target:  # the worktree root was resolved already; swap it now
            os.rename(world.work, world.tmp / "work-moved")
            os.symlink(decoy, world.work)
        return out

    with patch.object(memory_files.os.path, "realpath", side_effect=racing_realpath):
        with pytest.raises(MemoryFileError) as ei:
            memory_files.read_memory_file(target, loaded=world.loaded, cwd=cwd,
                                          config_dirs=[str(world.cfg)])
    assert ei.value.code == MEMORY_FILE_UNKNOWN
    assert os.path.islink(world.work)  # the swap happened, and was not followed


@pytest.mark.asyncio
async def test_viewer_caps_at_256_kb(world) -> None:
    out = await world.orch.memory_file(SID, path=str(world.big))
    assert out["truncated"] is True
    assert len(out["text"]) == MAX_MEMORY_BYTES == 256 * 1024


@pytest.mark.asyncio
async def test_viewer_unknown_session_is_a_keyerror(world) -> None:
    with pytest.raises(KeyError):
        await world.orch.memory_file("nope", path="/x")


@pytest.mark.asyncio
async def test_a_session_with_no_loaded_set_has_nothing_to_view(world) -> None:
    world.orch._records[SID].memory_files = None
    with pytest.raises(MemoryFileError):
        await world.orch.memory_file(SID, path=str(world.work / "CLAUDE.md"))


# ---- every file the CLI loads (review fix 1)


@pytest.fixture
def cli_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The layout the CLI really loads from: the account cfg dir links ``CLAUDE.md``,
    ``rules`` and ``projects`` to the shared base; the worktree sits below an ancestor
    with its own walk files; a user import lives outside every root."""
    home = tmp_path / "home"
    shared = home / ".claude"
    (shared / "projects" / "slug" / "memory").mkdir(parents=True)
    (shared / "rules").mkdir()
    (shared / "CLAUDE.md").write_text("u\n")
    (shared / "projects" / "slug" / "memory" / "MEMORY.md").write_text("m\n")
    (shared / "projects" / "slug" / "transcript.jsonl").write_text("secret\n")
    (shared / "rules" / "r.md").write_text("r\n")
    cfg = tmp_path / "store" / "cfg" / "acct"
    cfg.mkdir(parents=True)
    for n in ("CLAUDE.md", "projects", "rules"):
        (cfg / n).symlink_to(shared / n)
    anc = home / "GitHub"
    (anc / ".claude" / "rules" / "deep").mkdir(parents=True)
    (anc / "CLAUDE.md").write_text("anc\n")
    (anc / "CLAUDE.local.md").write_text("anc-local\n")
    (anc / ".claude" / "CLAUDE.md").write_text("anc-dot\n")
    (anc / ".claude" / "rules" / "deep" / "a.md").write_text("anc-rule\n")
    (anc / "other.md").write_text("not a walk name\n")
    work = anc / "repo"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "CLAUDE.md").write_text("p\n")
    imp = home / "notes" / "homeimport.md"
    imp.parent.mkdir()
    imp.write_text("imported\n")
    projext = home / "notes" / "projext.md"
    projext.write_text("project import\n")
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVATE KEY\n")
    managed = tmp_path / "etc-claude-code"
    (managed / ".claude" / "rules").mkdir(parents=True)
    (managed / "CLAUDE.md").write_text("managed\n")
    monkeypatch.setattr(memory_files, "MANAGED_DIR", str(managed))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "store"))
    rows = {
        "user": (cfg / "CLAUDE.md", "User"),
        "user_rule": (cfg / "rules" / "r.md", "User"),
        "automem": (cfg / "projects" / "slug" / "memory" / "MEMORY.md", "AutoMem"),
        "ancestor": (anc / "CLAUDE.md", "Project"),
        "ancestor_dot": (anc / ".claude" / "CLAUDE.md", "Project"),
        "ancestor_rule": (anc / ".claude" / "rules" / "deep" / "a.md", "Project"),
        "ancestor_local": (anc / "CLAUDE.local.md", "Local"),
        "project": (work / "CLAUDE.md", "Project"),
        "managed": (managed / "CLAUDE.md", "Managed"),
        "user_import": (imp, "User"),
        # refused shapes, all in the set:
        "ancestor_other": (anc / "other.md", "Project"),
        "transcript": (cfg / "projects" / "slug" / "transcript.jsonl", "AutoMem"),
        # a Project-type import outside every root: only User imports get the literal walk
        "project_import_outside": (projext, "Project"),
    }
    loaded = [{"path": str(p), "type": k, "tokens": 1} for p, k in rows.values()]
    return SimpleNamespace(rows=rows, loaded=loaded, cfg=cfg, work=work, imp=imp, ssh=ssh,
                           shared=shared, home=home)


def _read(w: SimpleNamespace, key: str) -> dict:
    path, _ = w.rows[key]
    return memory_files.read_memory_file(str(path), loaded=w.loaded, cwd=str(w.work),
                                         config_dirs=[str(w.cfg)])


@pytest.mark.parametrize(("key", "text"), [
    ("user", "u\n"), ("user_rule", "r\n"), ("automem", "m\n"), ("ancestor", "anc\n"),
    ("ancestor_dot", "anc-dot\n"), ("ancestor_rule", "anc-rule\n"),
    ("ancestor_local", "anc-local\n"), ("project", "p\n"), ("managed", "managed\n"),
    ("user_import", "imported\n"),
])
def test_viewer_reads_every_row_type_the_cli_loads(cli_world, key: str, text: str) -> None:
    out = _read(cli_world, key)
    assert out["text"] == text and out["type"] == cli_world.rows[key][1]
    assert out["editable"] is (key == "user")


@pytest.mark.parametrize("key", ["ancestor_other", "transcript", "project_import_outside"])
def test_viewer_still_refuses_set_entries_outside_the_cli_roots(cli_world, key: str) -> None:
    with pytest.raises(MemoryFileError) as ei:
        _read(cli_world, key)
    assert ei.value.code == MEMORY_FILE_UNKNOWN


def test_a_user_import_swapped_for_a_link_to_ssh_is_refused(cli_world) -> None:
    cli_world.imp.unlink()
    cli_world.imp.symlink_to(cli_world.ssh / "id_ed25519")
    with pytest.raises(MemoryFileError):
        _read(cli_world, "user_import")


def test_a_user_import_under_a_symlinked_directory_is_refused(cli_world) -> None:
    notes = cli_world.imp.parent
    os.rename(notes, cli_world.home / "notes-real")
    notes.symlink_to(cli_world.home / "notes-real")
    with pytest.raises(MemoryFileError):
        _read(cli_world, "user_import")


def test_an_ancestor_walk_name_linked_outside_is_refused(cli_world) -> None:
    anc_md = cli_world.rows["ancestor"][0]
    anc_md.unlink()
    anc_md.symlink_to(cli_world.ssh / "id_ed25519")
    with pytest.raises(MemoryFileError):
        _read(cli_world, "ancestor")


def test_editable_follows_the_shared_file_being_a_regular_file(cli_world, tmp_path) -> None:
    """Review fix 4: the editor refuses a symlinked shared file, so the viewer does not
    offer Edit for it either."""
    real = tmp_path / "dotfiles-CLAUDE.md"
    real.write_text("dot\n")
    (cli_world.shared / "CLAUDE.md").unlink()
    (cli_world.shared / "CLAUDE.md").symlink_to(real)
    out = _read(cli_world, "user")
    assert out["text"] == "dot\n" and out["editable"] is False


# ---- the route


def _app_client(orch: SessionOrchestrator) -> TestClient:
    from server.app import create_app
    with patch.dict(os.environ, _ENV):
        app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_route_returns_the_file(world) -> None:
    with patch.dict(os.environ, _ENV), _app_client(world.orch) as c:
        r = c.get(f"/api/sessions/{SID}/memory-file",
                  params={"path": str(world.cfg / "CLAUDE.md")}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["editable"] is True and r.json()["text"] == "# user rules\n"


def test_route_refusal_is_404_memory_file_unknown(world) -> None:
    with patch.dict(os.environ, _ENV), _app_client(world.orch) as c:
        r = c.get(f"/api/sessions/{SID}/memory-file", params={"path": str(world.secret)},
                  headers=_H)
        unknown = c.get("/api/sessions/nope/memory-file", params={"path": "/x"}, headers=_H)
        missing = c.get(f"/api/sessions/{SID}/memory-file", headers=_H)
        anon = c.get(f"/api/sessions/{SID}/memory-file", params={"path": "/x"})
    assert r.status_code == 404 and r.json()["detail"] == {"code": MEMORY_FILE_UNKNOWN}
    assert unknown.status_code == 404
    assert missing.status_code == 422
    assert anon.status_code == 401


def test_route_old_daemon_is_409_daemon_outdated(world) -> None:
    with patch.dict(os.environ, _ENV), _app_client(world.orch) as c, \
            patch.object(SessionOrchestrator, "memory_file",
                         side_effect=DaemonError("UnknownMethod", "memory_file")):
        r = c.get(f"/api/sessions/{SID}/memory-file", params={"path": "/x"}, headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


# ---- remote / RPC / fleet (Review Focus 5)


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
async def test_remote_forwards_to_the_daemon() -> None:
    client = _RecordingClient(result={"text": "x"})
    assert await RemoteOrchestrator(client).memory_file(SID, path="/a") == {"text": "x"}
    assert client.calls == [("memory_file", {"session_id": SID, "path": "/a"},
                             WORKTREE_RPC_TIMEOUT)]


@pytest.mark.asyncio
async def test_remote_rebuilds_errors_and_keeps_an_old_daemon_a_daemon_error() -> None:
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "nope"))).memory_file("nope", path="/a")
    with pytest.raises(MemoryFileError) as ei:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "MemoryFileError", MEMORY_FILE_UNKNOWN, {"code": MEMORY_FILE_UNKNOWN}))
        ).memory_file(SID, path="/a")
    assert ei.value.code == MEMORY_FILE_UNKNOWN
    with pytest.raises(DaemonError) as old:
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("UnknownMethod", "memory_file"))).memory_file(SID, path="/a")
    assert old.value.err_type == "UnknownMethod"
    with pytest.raises(DaemonError):  # an unknown code is never invented into a 404
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "MemoryFileError", "WAT", {"code": "WAT"}))).memory_file(SID, path="/a")


@pytest.mark.asyncio
async def test_uds_round_trip_reads_on_the_owner_daemon(
        world, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="server.ipc.daemon")
    app = create_daemon_app()
    register_orchestrator_rpc(app, world.orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            ok = await ro.memory_file(SID, path=str(world.cfg / "CLAUDE.md"))
            with pytest.raises(MemoryFileError) as refused:
                await ro.memory_file(SID, path=str(world.work / "evil.md"))
            with pytest.raises(KeyError):
                await ro.memory_file("nope", path="/x")
        finally:
            await client.aclose()
    assert ok["text"] == "# user rules\n" and ok["editable"] is True
    assert refused.value.code == MEMORY_FILE_UNKNOWN
    rpc_errors = [r for r in caplog.records
                  if r.name == "server.ipc.daemon" and "rpc.error" in r.getMessage()]
    assert len(rpc_errors) == 2
    assert all(r.levelno == logging.INFO for r in rpc_errors), \
        [(r.levelname, r.getMessage()) for r in rpc_errors]


@pytest.mark.asyncio
async def test_fleet_routes_the_read_to_the_owning_host_and_facade_forwards() -> None:
    assert "memory_file" in _PER_SESSION

    class _HostOrch:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def memory_file(self, session_id, **kw):
            self.calls.append((session_id, kw))
            return {"host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    assert await fo.memory_file(SID, path="/a") == {"host": "rocky"}
    assert rocky.calls == [(SID, {"path": "/a"})] and dobby.calls == []
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert await adapter.memory_file(SID, path="/b") == {"host": "dobby"}


# ---------------------------------------------------------------- the editor


@pytest.fixture
def shared_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``shared_base()`` (through ``CLAUDE_CONFIG_DIR``) is a temp dir; the config
    auto-snapshot is recorded instead of taken."""
    shared = tmp_path / "dot-claude"
    shared.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "store"))
    snaps: list[int] = []
    monkeypatch.setattr("server.api.memory.snapshot_before_mutation",
                        lambda: snaps.append(1))
    return SimpleNamespace(shared=shared, file=shared / "CLAUDE.md", snaps=snaps)


@pytest.fixture
def editor(shared_base, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The app on a file-backed DB (so the audit row is readable), over ``shared_base``."""
    from server.app import create_app
    monkeypatch.setenv("VIBERPIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setenv("VIBERPIT_DEV", "1")
    monkeypatch.setenv("VIBERPIT_TAILNET_ALLOWLIST", "")
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    client = TestClient(create_app(orchestrator=orch), raise_server_exceptions=False)
    with client:
        yield SimpleNamespace(c=client, shared=shared_base.shared, file=shared_base.file,
                              snaps=shared_base.snaps)


PROPAGATION = "Sessions on other hosts can take up to 10 minutes to see this change"


def _etag(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_missing_file_reads_as_not_existing_with_empty_etag(editor) -> None:
    r = editor.c.get("/api/settings/memory", headers=_H)
    assert r.status_code == 200
    assert r.json() == {"text": "", "etag": "", "exists": False}


def test_save_creates_the_file_0644_and_returns_its_etag(editor) -> None:
    r = editor.c.put("/api/settings/memory", json={"text": "hello\n", "etag": ""}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"etag": _etag(b"hello\n"), "propagation": PROPAGATION}
    assert editor.file.read_bytes() == b"hello\n"
    assert stat.S_IMODE(editor.file.stat().st_mode) == 0o644
    assert editor.snaps == [1]
    assert editor.c.get("/api/settings/memory", headers=_H).json() == {
        "text": "hello\n", "etag": _etag(b"hello\n"), "exists": True}
    # no temp file left beside it
    assert sorted(p.name for p in editor.shared.iterdir()) == ["CLAUDE.md"]


def test_a_save_keeps_the_existing_file_mode(editor) -> None:
    """A user who tightened their CLAUDE.md to 0600 must not find it world-readable
    after one save from Settings."""
    editor.file.write_bytes(b"v0\n")
    editor.file.chmod(0o600)
    r = editor.c.put("/api/settings/memory",
                     json={"text": "v1\n", "etag": _etag(b"v0\n")}, headers=_H)
    assert r.status_code == 200, r.text
    assert editor.file.read_bytes() == b"v1\n"
    assert stat.S_IMODE(editor.file.stat().st_mode) == 0o600


def test_two_tabs_second_save_conflicts_and_nothing_is_lost(
        editor, caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 3."""
    caplog.set_level(logging.INFO)
    editor.file.write_text("v0\n")
    tag0 = editor.c.get("/api/settings/memory", headers=_H).json()["etag"]
    first = editor.c.put("/api/settings/memory", json={"text": "tab A\n", "etag": tag0},
                         headers=_H)
    assert first.status_code == 200
    second = editor.c.put("/api/settings/memory", json={"text": "tab B\n", "etag": tag0},
                          headers=_H)
    assert second.status_code == 409
    assert second.json()["detail"] == {"code": "MEMORY_CONFLICT", "text": "tab A\n",
                                       "etag": _etag(b"tab A\n")}
    assert editor.file.read_text() == "tab A\n"
    # an etag-less create over an existing file is a conflict too
    assert editor.c.put("/api/settings/memory", json={"text": "x", "etag": ""},
                        headers=_H).status_code == 409
    assert _no_warnings(caplog) == []


def test_a_symlinked_user_file_is_refused_and_left_alone(
        editor, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    target = tmp_path / "dotfiles-CLAUDE.md"
    target.write_text("linked\n")
    editor.file.symlink_to(target)
    g = editor.c.get("/api/settings/memory", headers=_H)
    p = editor.c.put("/api/settings/memory", json={"text": "x", "etag": _etag(b"linked\n")},
                     headers=_H)
    assert g.status_code == 409 and g.json()["detail"]["code"] == "MEMORY_FILE_IS_LINK"
    assert p.status_code == 409 and p.json()["detail"]["code"] == "MEMORY_FILE_IS_LINK"
    assert editor.file.is_symlink() and target.read_text() == "linked\n"
    assert _no_warnings(caplog) == []


def test_over_256_kb_is_413(editor) -> None:
    r = editor.c.put("/api/settings/memory",
                     json={"text": "a" * (MAX_MEMORY_BYTES + 1), "etag": ""}, headers=_H)
    assert r.status_code == 413
    assert not editor.file.exists()
    ok = editor.c.put("/api/settings/memory", json={"text": "a" * MAX_MEMORY_BYTES,
                                                    "etag": ""}, headers=_H)
    assert ok.status_code == 200


def test_a_save_writes_an_audit_row(editor) -> None:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent
    editor.c.put("/api/settings/memory", json={"text": "hi", "etag": ""}, headers=_H)

    async def _rows():
        async with db_session() as db:
            return (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "settings.memory_saved"))).scalars().all()

    assert editor.c.portal is not None
    rows = editor.c.portal.call(_rows)
    assert len(rows) == 1
    assert rows[0].identity == "testuser@example.com"
    assert rows[0].detail == {"bytes": 2, "created": True}


def test_editor_requires_identity(editor) -> None:
    assert editor.c.get("/api/settings/memory").status_code == 401
    assert editor.c.put("/api/settings/memory", json={"text": "", "etag": ""}).status_code == 401


def test_a_crash_mid_write_leaves_the_old_file_intact(shared_base) -> None:
    """The rename is the commit point: a failure before it leaves the old bytes and
    removes the temp file."""
    from server.api import memory as memory_api
    shared_base.file.write_text("old\n")
    with patch("viberpit.fs_beneath.os.replace", side_effect=OSError("disk gone")):
        with pytest.raises(OSError):
            memory_api.write_user_memory("new\n", _etag(b"old\n"))
    assert shared_base.file.read_text() == "old\n"
    assert sorted(p.name for p in shared_base.shared.iterdir()) == ["CLAUDE.md"]


def test_the_write_is_fsynced_before_the_rename(shared_base) -> None:
    """No live app here: its background loops write files too, and the patch is global."""
    from server.api import memory as memory_api
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        order.append("fsync")
        real_fsync(fd)

    def replace(*a, **k):
        order.append("replace")
        real_replace(*a, **k)

    with patch("viberpit.fs_beneath.os.fsync", side_effect=fsync), \
            patch("viberpit.fs_beneath.os.replace", side_effect=replace):
        memory_api.write_user_memory("x", "")
    assert order == ["fsync", "replace", "fsync"], order


def test_a_non_utf8_file_is_refused_for_editing_not_mangled(editor) -> None:
    """Review fix 3: a lossy decode would let a save silently rewrite the bytes."""
    editor.file.write_bytes(b"caf\xe9\n")
    g = editor.c.get("/api/settings/memory", headers=_H)
    assert g.status_code == 422 and g.json()["detail"] == {"code": "MEMORY_NOT_UTF8"}
    p = editor.c.put("/api/settings/memory",
                     json={"text": "x", "etag": _etag(b"caf\xe9\n")}, headers=_H)
    assert p.status_code == 422
    assert editor.file.read_bytes() == b"caf\xe9\n"
