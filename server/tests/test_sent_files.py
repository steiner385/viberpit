"""`SendUserFile` ingest (#3487): the files a session sent the user, made viewable.

The CLI's ``SendUserFile`` uploads to claude.ai, which a ViberPit user never sees, and
agents write those files under ``/tmp`` — outside the worktree the browser can read.
``POST /api/sessions/{id}/sent-files/{tool_use_id}`` has the daemon that owns the
session ingest each image the CALL named into the attachment store, as
`share_artifact` kind ``image`` does. The paths come from the session's own replay,
never the request.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import struct
import zlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from server.db import db_session
from server.db.models import Attachment, Session as DbSession
from server.ipc.client import DaemonError
from server.sessions.events import ToolCall, ToolKind
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.replay import ReplayWindow
from server.sessions import sent_files as sent_files_mod
from server.sessions.sent_files import (HISTORY_UNAVAILABLE, MAX_SENT_FILES,
                                        TOOL_CALL_NOT_FOUND, SentFileError,
                                        _clear_index_cache, scan_send_user_file_calls,
                                        send_user_file_calls, sent_file_paths)
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_sessions_actions_api import _ENV, _H, _make_client

SID = "abcd1234-5678-0000-0000-000000000000"
TOOL = "toolu_send1"


def _png(width: int = 2, height: int = 1) -> bytes:
    """A real (tiny) PNG, built rather than embedded."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _call(files, *, tool_id=TOOL, name="SendUserFile", parent=None) -> ToolCall:
    return ToolCall(turn_id="t1", tool_id=tool_id, name=name, kind=ToolKind.UNKNOWN,
                    input={"files": files, "status": "normal"}, parent_tool_use_id=parent)


def _factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
             options_overlay=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


def _jsonl(events, path) -> str:
    """The CLI's transcript shape for these calls: one assistant entry per call, a
    sub-agent's flagged ``isSidechain`` (Layout A), plus unrelated noise lines."""
    lines = ['{"type":"user","uuid":"u0","message":{"content":"hi"}}', "not json SendUserFile"]
    for i, e in enumerate(events):
        lines.append(json.dumps({
            "type": "assistant", "uuid": f"a{i}", "isSidechain": e.parent_tool_use_id is not None,
            "message": {"content": [{"type": "text", "text": "sending"},
                                    {"type": "tool_use", "id": e.tool_id, "name": e.name,
                                     "input": e.input}]}}))
    Path(path).write_text("\n".join(lines) + "\n")
    return str(path)


async def _orch(events, *, cwd: str | None, archived: bool = True,
                replay: bool = False) -> SessionOrchestrator:
    """An orchestrator whose session SID has these calls in its JSONL — or, with
    ``replay=True``, no transcript file and these events from the replay fallback
    (``None``: no readable history at all)."""
    async with db_session() as db:
        db.add(DbSession(id=SID, name="s", account_name="acct1", engine_session_id="cabc",
                         status="archived" if archived else "idle", cwd=cwd or "",
                         archived_at=datetime(2026, 11, 25) if archived else None))
    orch = SessionOrchestrator(sdk_factory=_factory)
    transcript = None
    if not replay:
        transcript = _jsonl(events, Path(tempfile.mkdtemp(prefix="sent-files-")) / "t.jsonl")

    async def resolve_transcript_path(session_id):
        assert session_id == SID
        return transcript

    async def transcript_window(session_id, **kw):
        assert replay, "the JSONL scan must not fall back to a full replay"
        assert session_id == SID and kw.get("limit") is None
        return None if events is None else ReplayWindow(events=list(events), total_turns=1)

    orch.resolve_transcript_path = resolve_transcript_path  # type: ignore[method-assign]
    orch.transcript_window = transcript_window  # type: ignore[method-assign]
    return orch


@pytest.fixture(autouse=True)
def _fresh_index():
    _clear_index_cache()
    yield
    _clear_index_cache()


async def _attachment_count() -> int:
    async with db_session() as db:
        return (await db.execute(select(func.count()).select_from(Attachment))).scalar_one()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    return tmp_path


# ── finding the call ─────────────────────────────────────────────────────────


def test_only_the_main_thread_send_user_file_call_with_that_id_is_found():
    events = [_call(["/tmp/sub.png"], parent="toolu_task"),
              _call(["/tmp/other.png"], name="Write"),
              _call(["/tmp/a.png", "", 7, "b.png"])]
    assert sent_file_paths(events, TOOL) == ["/tmp/a.png", "b.png"]
    assert sent_file_paths([e.to_wire() for e in events], TOOL) == ["/tmp/a.png", "b.png"]
    assert sent_file_paths(events[:2], TOOL) is None
    assert sent_file_paths(events, "toolu_unknown") is None


# ── the orchestrator ingest (runs on the owning daemon) ──────────────────────


@pytest.mark.asyncio
async def test_a_tmp_image_is_ingested_and_served_from_the_attachment_store(isolated_db,
                                                                            home):
    from server.sessions.attachments import storage_path
    shot = home / "tmp" / "shot.png"
    shot.parent.mkdir()
    shot.write_bytes(_png())
    orch = await _orch([_call([str(shot)])], cwd=str(home / "wt"))
    out = await orch.sent_files(SID, TOOL)
    async with db_session() as db:
        row = (await db.execute(select(Attachment))).scalar_one()
    assert out == {"files": [{"path": str(shot), "url": f"/api/attachments/{row.id}",
                              "mediaType": "image/png", "reason": None}]}
    assert row.session_id == SID and row.filename == "shot.png"
    assert storage_path(row.sha256, row.media_type).read_bytes() == _png()


@pytest.mark.asyncio
async def test_a_relative_path_resolves_against_the_session_cwd(isolated_db, home):
    wt = home / "wt"
    (wt / "out").mkdir(parents=True)
    (wt / "out" / "chart.png").write_bytes(_png())
    orch = await _orch([_call(["out/chart.png"])], cwd=str(wt))
    [entry] = (await orch.sent_files(SID, TOOL))["files"]
    assert entry["path"] == "out/chart.png" and entry["url"].startswith("/api/attachments/")


@pytest.mark.asyncio
async def test_a_relative_path_with_no_cwd_is_refused(isolated_db, home):
    orch = await _orch([_call(["chart.png"])], cwd=None)
    [entry] = (await orch.sent_files(SID, TOOL))["files"]
    assert entry == {"path": "chart.png", "url": None, "mediaType": None, "reason": "no_cwd"}


@pytest.mark.asyncio
async def test_non_images_and_missing_files_answer_a_reason_not_a_url(isolated_db, home,
                                                                     caplog):
    notes = home / "notes.png"
    notes.write_text("text with an image extension")
    orch = await _orch([_call([str(notes), str(home / "gone.png"), str(home)])], cwd=None)
    with caplog.at_level("INFO"):
        files = (await orch.sent_files(SID, TOOL))["files"]
    assert [(f["url"], f["reason"]) for f in files] == [
        (None, "not_image"), (None, "not_found"), (None, "not_a_file")]
    assert await _attachment_count() == 0
    assert not [r for r in caplog.records if r.levelno >= 30]


@pytest.mark.asyncio
async def test_an_oversize_image_is_refused(isolated_db, home):
    from server.sessions.attachments import MAX_BYTES
    big = home / "big.png"
    big.write_bytes(_png() + b"\x00" * MAX_BYTES)
    orch = await _orch([_call([str(big)])], cwd=None)
    [entry] = (await orch.sent_files(SID, TOOL))["files"]
    assert entry["url"] is None and entry["reason"] == "too_large"


@pytest.mark.asyncio
async def test_files_past_the_cap_are_not_read(isolated_db, home):
    shot = home / "shot.png"
    shot.write_bytes(_png())
    orch = await _orch([_call([str(shot)] * (MAX_SENT_FILES + 1))], cwd=None)
    files = (await orch.sent_files(SID, TOOL))["files"]
    assert len(files) == MAX_SENT_FILES + 1
    assert files[-1]["reason"] == "too_many" and files[0]["url"] is not None


@pytest.mark.asyncio
async def test_the_ingest_is_idempotent(isolated_db, home):
    shot = home / "shot.png"
    shot.write_bytes(_png())
    orch = await _orch([_call([str(shot)])], cwd=None)
    first = await orch.sent_files(SID, TOOL)
    shot.unlink()  # a repeat answers the saved row without re-reading the file
    second = await orch.sent_files(SID, TOOL)
    assert first == second and first["files"][0]["url"] is not None
    assert await _attachment_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("events", [
    [_call(["/tmp/x.png"], parent="toolu_task")],       # a sub-agent's call
    [_call(["/tmp/x.png"], name="Write")],              # another tool's id
    [_call(["/tmp/x.png"], tool_id="toolu_other")],     # an unknown id
])
async def test_no_main_thread_send_user_file_call_is_a_tool_call_not_found(isolated_db, home,
                                                                          events, replay):
    orch = await _orch(events, cwd=None, replay=replay)
    with pytest.raises(SentFileError) as err:
        await orch.sent_files(SID, TOOL)
    assert err.value.code == TOOL_CALL_NOT_FOUND
    assert err.value.error_data == {"code": TOOL_CALL_NOT_FOUND}


@pytest.mark.asyncio
async def test_an_engine_without_a_transcript_file_falls_back_to_the_replay(isolated_db, home):
    shot = home / "shot.png"
    shot.write_bytes(_png())
    orch = await _orch([_call([str(shot)])], cwd=None, replay=True)
    [entry] = (await orch.sent_files(SID, TOOL))["files"]
    assert entry["url"].startswith("/api/attachments/")


@pytest.mark.asyncio
async def test_no_readable_history_is_retryable_not_a_missing_call(isolated_db, home):
    orch = await _orch(None, cwd=None, replay=True)
    with pytest.raises(SentFileError) as err:
        await orch.sent_files(SID, TOOL)
    assert err.value.code == HISTORY_UNAVAILABLE


# ── the transcript scan ──────────────────────────────────────────────────────


def test_the_scan_indexes_main_thread_send_user_file_calls_only(tmp_path):
    path = _jsonl([_call(["/tmp/a.png", "", 3]), _call(["/tmp/s.png"], tool_id="t_sub",
                                                         parent="toolu_task"),
                   _call(["/tmp/w.png"], tool_id="t_write", name="Write"),
                   _call(["b.png"], tool_id="t2")], tmp_path / "t.jsonl")
    assert scan_send_user_file_calls(path) == {TOOL: ["/tmp/a.png"], "t2": ["b.png"]}


@pytest.mark.asyncio
async def test_a_slow_scan_is_history_unavailable_and_the_retry_joins_it(tmp_path, monkeypatch):
    path = _jsonl([_call(["/tmp/a.png"])], tmp_path / "t.jsonl")
    real, runs = scan_send_user_file_calls, []

    def slow(p):
        runs.append(p)
        time.sleep(0.3)
        return real(p)

    monkeypatch.setattr(sent_files_mod, "scan_send_user_file_calls", slow)
    with pytest.raises(SentFileError) as err:
        await send_user_file_calls(path, ceiling_s=0.05)
    assert err.value.code == HISTORY_UNAVAILABLE
    assert (await send_user_file_calls(path, ceiling_s=5))[TOOL] == ["/tmp/a.png"]
    assert len(runs) == 1  # the retry joined the scan in flight


@pytest.mark.asyncio
async def test_concurrent_lookups_share_one_scan_cached_until_the_file_changes(tmp_path,
                                                                             monkeypatch):
    path = _jsonl([_call(["/tmp/a.png"])], tmp_path / "t.jsonl")
    real, runs = scan_send_user_file_calls, []

    def counted(p):
        runs.append(p)
        return real(p)

    monkeypatch.setattr(sent_files_mod, "scan_send_user_file_calls", counted)
    results = await asyncio.gather(*(send_user_file_calls(path) for _ in range(5)))
    assert all(r == {TOOL: ["/tmp/a.png"]} for r in results) and len(runs) == 1
    await send_user_file_calls(path)
    assert len(runs) == 1
    # The live race: the call lands on disk after the first lookup.
    _jsonl([_call(["/tmp/a.png"]), _call(["/tmp/b.png"], tool_id="t_late")], path)
    assert (await send_user_file_calls(path))["t_late"] == ["/tmp/b.png"]
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_an_unreadable_transcript_is_history_unavailable(tmp_path):
    with pytest.raises(SentFileError) as err:
        await send_user_file_calls(str(tmp_path / "gone.jsonl"))
    assert err.value.code == HISTORY_UNAVAILABLE


@pytest.mark.asyncio
async def test_an_unknown_session_is_a_key_error_marked_as_an_expected_refusal(isolated_db):
    orch = SessionOrchestrator(sdk_factory=_factory)
    with pytest.raises(KeyError) as err:
        await orch.sent_files("nope", TOOL)
    assert getattr(err.value, "error_data", None) == {"code": "SESSION_NOT_FOUND"}


# ── the route ────────────────────────────────────────────────────────────────


def _route_client(result=None, error: Exception | None = None):
    client, fake = _make_client()
    calls: list[tuple] = []

    async def sent_files(*args, **kwargs):
        calls.append((args, kwargs))
        if error is not None:
            raise error
        return result

    fake.sent_files = sent_files
    return client, calls


def test_route_returns_the_daemons_answer_and_never_takes_a_path_from_the_request():
    body = {"files": [{"path": "/tmp/a.png", "url": "/api/attachments/x",
                       "mediaType": "image/png", "reason": None}]}
    client, calls = _route_client(result=body)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sent-files/{TOOL}?path=/etc/passwd",
                        json={"files": ["/etc/passwd"], "path": "/etc/passwd"}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json() == body
    assert calls == [((SID, TOOL), {})]


def test_route_404s_a_missing_call_and_logs_info(caplog):
    client, _ = _route_client(error=SentFileError(TOOL_CALL_NOT_FOUND))
    with patch.dict(os.environ, _ENV), client, caplog.at_level("INFO"):
        r = client.post(f"/api/sessions/{SID}/sent-files/{TOOL}", headers=_H)
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": TOOL_CALL_NOT_FOUND}
    hits = [rec for rec in caplog.records if "sent files" in rec.getMessage()]
    assert hits and all(rec.levelname == "INFO" for rec in hits)


def test_route_503s_unavailable_history_so_the_client_retries():
    client, _ = _route_client(error=SentFileError(HISTORY_UNAVAILABLE))
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sent-files/{TOOL}", headers=_H)
    assert r.status_code == 503
    assert r.json()["detail"] == {"code": HISTORY_UNAVAILABLE}


def test_route_404s_an_unknown_session():
    client, _ = _route_client(error=KeyError(SID))
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sent-files/{TOOL}", headers=_H)
    assert r.status_code == 404


def test_route_maps_an_old_daemon_to_409_daemon_outdated():
    client, _ = _route_client(error=DaemonError("UnknownMethod", "sent_files"))
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/sent-files/{TOOL}", headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "daemon_outdated"


# ── RPC / remote / facade / fleet ────────────────────────────────────────────


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_and_rebuilds_its_refusals():
    client = _RecordingClient(result={"files": []})
    assert await RemoteOrchestrator(client).sent_files(SID, TOOL) == {"files": []}
    assert client.calls == [("sent_files", {"session_id": SID, "tool_use_id": TOOL})]
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "nope"))).sent_files("nope", TOOL)
    with pytest.raises(SentFileError):
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "SentFileError", TOOL_CALL_NOT_FOUND,
            data={"code": TOOL_CALL_NOT_FOUND}))).sent_files(SID, TOOL)
    with pytest.raises(SentFileError) as err:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "SentFileError", HISTORY_UNAVAILABLE,
            data={"code": HISTORY_UNAVAILABLE}))).sent_files(SID, TOOL)
    assert err.value.code == HISTORY_UNAVAILABLE
    with pytest.raises(DaemonError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("UnknownMethod", "sent_files"))).sent_files(SID, TOOL)


def test_the_ingest_crosses_the_real_daemon_envelope(tmp_path, monkeypatch):
    """Edge → RemoteOrchestrator → RPC → the owning daemon's real orchestrator: the
    image is read and stored THERE, and both refusals come back typed."""
    from server.db import init_db
    from server.ipc.client import DaemonClient
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.tests.ipc_harness import serve_daemon_uds

    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        orch = await _orch([_call([str(shot)])], cwd=None)
        app = create_daemon_app()
        register_orchestrator_rpc(app, orch)
        async with serve_daemon_uds(app) as sock:
            client = DaemonClient(sock)
            try:
                ro = RemoteOrchestrator(client)
                out = await ro.sent_files(SID, TOOL)
                with pytest.raises(SentFileError):
                    await ro.sent_files(SID, "toolu_unknown")
                with pytest.raises(KeyError):
                    await ro.sent_files("nope", TOOL)
            finally:
                await client.aclose()
        return out

    [entry] = asyncio.run(run())["files"]
    assert entry["url"].startswith("/api/attachments/") and entry["mediaType"] == "image/png"


class _HostOrch:
    def __init__(self, name):
        self.name, self.calls = name, []

    async def sent_files(self, sid, tool_use_id):
        self.calls.append((sid, tool_use_id))
        return {"host": self.name}


class _Router:
    def __init__(self, orchs):
        self._orchs = orchs

    def get(self, name):
        return self._orchs[name]

    def names(self):
        return sorted(self._orchs)


def test_fleet_routes_to_the_host_that_owns_the_session(isolated_db):
    assert "sent_files" in _PER_SESSION and "sent_files" not in _MOVE_BLOCKED

    async def run():
        async with db_session() as db:
            db.add(DbSession(id=SID, name="s", account_name="acct1", engine_session_id="c",
                             status="archived", cwd="/w", host="rocky",
                             archived_at=datetime(2026, 11, 25)))
        dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
        fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}),  # type: ignore[arg-type]
                               primary="dobby")
        return await fo.sent_files(SID, TOOL), dobby.calls, rocky.calls

    out, dobby_calls, rocky_calls = asyncio.run(run())
    assert out == {"host": "rocky"}
    assert rocky_calls == [(SID, TOOL)] and dobby_calls == []


@pytest.mark.asyncio
async def test_facade_forwards():
    adapter = InProcessOrchestratorAdapter(_HostOrch("dobby"))  # type: ignore[arg-type]
    assert await adapter.sent_files(SID, TOOL) == {"host": "dobby"}
