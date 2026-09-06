"""Transcript export (#1277) and transcript search (#1278) for Codex sessions.

Both gaps had the same shape: an engine-neutral capability gated behind a
Claude-shaped question. ``transcript_path``/``resolve_transcript_path`` ask "where
is the JSONL?", which is None for every Codex session by construction — so the
Markdown export 404'd before reaching the replay that already speaks both engines,
and the search extractor returned "" so Codex conversations were invisible to search.

The invariants pinned here:

* an existence check that both engines can answer (``has_transcript``),
* Markdown export and search drawing on the ONE canonical normalized replay,
* the machine-readable export staying honest — Codex gets declared normalized
  events, never a Claude-native transcript's clothes,
* Claude's behaviour, including its cheap direct file read on the search path,
  unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.engine import AgentDriverRegistry, AgentEngine, AgentCapabilities
from server.sessions.export import NORMALIZED_EXPORT_FORMAT, normalized_ndjson
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.search import events_text

from server.tests.test_codex_orchestrator import (
    _FakeCodexManager,
    _FakeThreadClient,
    _RecFactory,
    _record,
    _turn,
)

_IDENT = {"Tailscale-User-Login": "apitest@example.com"}


def _registry() -> AgentDriverRegistry:
    return AgentDriverRegistry({
        AgentEngine.CLAUDE_CODE: _RecFactory(
            AgentEngine.CLAUDE_CODE,
            capabilities=AgentCapabilities(adopt=True, account_rotation=True)),
        AgentEngine.CODEX: _RecFactory(AgentEngine.CODEX),
    })


def _orch(tmp_path: Path, *, codex=None) -> SessionOrchestrator:
    repo_root = tmp_path / "repos"
    repo_root.mkdir(exist_ok=True)
    return SessionOrchestrator(sdk_factory=lambda *a, **k: None, codex=codex,
                               driver_registry=_registry(), repo_root=str(repo_root),
                               poll_interval=9999.0)


def _text_turn(turn_id: str, user: str, agent: str) -> dict:
    return {"id": turn_id, "status": "completed", "items": [
        {"type": "userMessage", "id": f"{turn_id}-u",
         "content": [{"type": "text", "text": user}]},
        {"type": "agentMessage", "id": f"{turn_id}-a", "text": agent},
    ]}


async def _seed_archived_row(tmp_path: Path, session_id: str, *,
                             thread_id: str = "thread-1") -> None:
    """An archived Codex row: present in the DB, absent from ``_records``."""
    cwd = tmp_path / session_id
    cwd.mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        db.add(DbSession(
            id=session_id, name="archived codex", account_name="openai-main",
            engine=AgentEngine.CODEX.value, engine_session_id=thread_id,
            cwd=str(cwd), status="archived",
        ))
        await db.commit()


# --------------------------------------------------------------------------- #
# events_text — the engine-neutral extractor (#1278)                            #
# --------------------------------------------------------------------------- #


def test_events_text_selects_the_same_content_transcript_text_does():
    """User prompts, assistant text and thinking; never tool JSON. Matching a term
    inside a tool's arguments is not the user finding their own conversation."""
    text = events_text([
        {"type": "sessionMeta", "model": "gpt-5-codex"},
        {"type": "user", "text": "deploy the widget"},
        {"type": "turnStart", "turnId": "a0"},
        {"type": "thinkingDelta", "turnId": "a0", "text": "pondering widgets"},
        {"type": "textDelta", "turnId": "a0", "text": "widget deployed"},
        {"type": "toolCall", "turnId": "a0", "name": "Bash",
         "input": {"command": "rm -rf /secretmarker"}},
        {"type": "toolResult", "toolId": "t1", "ok": True,
         "payload": {"content": "othersecretmarker"}},
    ])
    assert "deploy the widget" in text
    assert "pondering widgets" in text
    assert "widget deployed" in text
    assert "secretmarker" not in text


def test_events_text_drops_sub_agent_output():
    """The normalized counterpart of the `isSidechain` entries transcript_text skips:
    indexing them makes a session match text its operator never saw."""
    text = events_text([
        {"type": "textDelta", "turnId": "a0", "text": "main thread"},
        {"type": "textDelta", "turnId": "a1", "text": "sidechain",
         "parentToolUseId": "tool-7"},
    ])
    assert text == "main thread"


def test_events_text_tolerates_junk():
    """A caller hands over whatever the replay produced; it must not have to
    pre-validate it."""
    assert events_text(None) == ""
    assert events_text([]) == ""
    assert events_text(["nope", None, 7, {"type": "user"},
                        {"type": "user", "text": None},
                        {"type": "user", "text": "real"}]) == "real"


# --------------------------------------------------------------------------- #
# normalized_ndjson — the honest machine-readable export (#1277)                #
# --------------------------------------------------------------------------- #


def test_normalized_ndjson_declares_what_it_is_on_line_one():
    out = normalized_ndjson(
        [{"type": "user", "text": "hi"}, {"type": "textDelta", "text": "hello"}],
        session_id="s1", engine="codex", model="gpt-5-codex")
    lines = [json.loads(x) for x in out.strip().split("\n")]
    header = lines[0]
    assert header["type"] == "ccswitchExportMeta"
    assert header["format"] == NORMALIZED_EXPORT_FORMAT
    assert header["engine"] == "codex"
    assert header["sessionId"] == "s1"
    assert header["model"] == "gpt-5-codex"
    # The honesty requirement of #1277: a consumer must be able to tell on line 1
    # that this is NOT a Claude-native transcript.
    assert "NOT a native Claude Code transcript" in header["note"]
    assert lines[1:] == [{"type": "user", "text": "hi"},
                         {"type": "textDelta", "text": "hello"}]


def test_normalized_ndjson_is_valid_ndjson_when_empty():
    out = normalized_ndjson([], session_id="s1", engine="codex")
    assert out.endswith("\n")
    assert len([x for x in out.strip().split("\n")]) == 1


# --------------------------------------------------------------------------- #
# has_transcript — the engine-neutral existence gate (#1277)                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_has_transcript_is_true_for_a_live_codex_session(tmp_path: Path) -> None:
    """transcript_path answers None here by design; has_transcript must not."""
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=_FakeThreadClient([_turn("t1")])))
    orch._records["s1"] = _record("s1", tmp_path)
    assert orch.transcript_path("s1") is None
    assert await orch.has_transcript("s1") is True


@pytest.mark.asyncio
async def test_has_transcript_is_false_when_the_codex_runner_is_unreachable(
        tmp_path: Path) -> None:
    """An unreachable runner is truthfully "no readable history" — a 404, not a 500."""
    manager = _FakeCodexManager(live=False, ensure_raises=RuntimeError("no runner"))
    orch = _orch(tmp_path, codex=manager)
    orch._records["s1"] = _record("s1", tmp_path)
    assert await orch.has_transcript("s1") is False


@pytest.mark.asyncio
async def test_has_transcript_follows_the_claude_path(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    with patch.object(orch, "resolve_transcript_path", AsyncMock(return_value=None)):
        assert await orch.has_transcript("claude-1") is False
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text("")
    with patch.object(orch, "resolve_transcript_path", AsyncMock(return_value=str(jsonl))):
        assert await orch.has_transcript("claude-1") is True


@pytest.mark.asyncio
async def test_has_transcript_never_raises(tmp_path: Path) -> None:
    """It only ever refines a 404; a lookup blowing up must not become a 500."""
    orch = _orch(tmp_path)
    with patch.object(orch, "_engine_replay", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await orch.has_transcript("s1") is False


# --------------------------------------------------------------------------- #
# transcript_search_text (#1278)                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_text_reads_a_live_codex_thread(tmp_path: Path, isolated_db) -> None:
    client = _FakeThreadClient([_text_turn("t1", "find the flux capacitor", "found it")])
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    orch._records["s1"] = _record("s1", tmp_path)

    text = await orch.transcript_search_text("s1")
    assert "flux capacitor" in text
    assert "found it" in text


@pytest.mark.asyncio
async def test_search_text_reads_an_archived_codex_row(tmp_path: Path, isolated_db) -> None:
    """Archived rows are not in `_records`, and search covers the archived scope —
    the engine has to come off the DB row or the whole scope stays unsearchable."""
    client = _FakeThreadClient([_text_turn("t1", "archived question", "archived answer")])
    await init_db()
    await _seed_archived_row(tmp_path, "s1")
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    assert "s1" not in orch._records

    with patch.object(orch, "_find_transcript") as find:
        text = await orch.transcript_search_text("s1")
    # The Claude glob must never be consulted for a Codex row.
    find.assert_not_called()
    assert "archived question" in text and "archived answer" in text


@pytest.mark.asyncio
async def test_search_text_reads_the_full_history_not_a_trailing_page(
        tmp_path: Path, isolated_db) -> None:
    """A default window is a TRAILING page, so a term would stop matching once the
    session grew past it — search silently forgetting things."""
    turns = [_text_turn(f"t{i}", f"question-{i}", f"answer-{i}") for i in range(40)]
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=_FakeThreadClient(turns)))
    orch._records["s1"] = _record("s1", tmp_path)

    text = await orch.transcript_search_text("s1")
    assert "question-0" in text          # the oldest turn is still findable
    assert "question-39" in text


@pytest.mark.asyncio
async def test_search_text_degrades_to_empty_when_replay_is_unavailable(
        tmp_path: Path, isolated_db) -> None:
    """One unreadable session must degrade to name-only matching, never fail the
    whole search (transcript_window returns None on timeout as well as on absence)."""
    orch = _orch(tmp_path, codex=_FakeCodexManager(live=False,
                                                   ensure_raises=RuntimeError("down")))
    orch._records["s1"] = _record("s1", tmp_path)
    assert await orch.transcript_search_text("s1") == ""


@pytest.mark.asyncio
async def test_search_text_keeps_claudes_direct_file_read(tmp_path: Path) -> None:
    """Claude must NOT be rerouted onto replay: transcript_text is a byte-capped scan
    with no sub-agent sidecar discovery, and this runs once per session, concurrently,
    for every search the user types."""
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "uuid": "u0", "message": {"content": "claude prompt"}},
        {"type": "assistant", "uuid": "a0", "message": {
            "model": "claude-x", "content": [{"type": "text", "text": "claude reply"}]}},
    ]))
    orch = _orch(tmp_path)
    with patch.object(orch, "resolve_transcript_path", AsyncMock(return_value=str(jsonl))), \
         patch.object(orch, "transcript_window", AsyncMock()) as window:
        text = await orch.transcript_search_text("claude-1")
    window.assert_not_called()
    assert "claude prompt" in text and "claude reply" in text


# --------------------------------------------------------------------------- #
# HTTP: export + search end to end                                              #
# --------------------------------------------------------------------------- #


def _codex_app(tmp_path: Path, turns: list[dict], *, session_id: str = "codex-1"):
    from server.app import create_app
    client = _FakeThreadClient(turns)
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=client))
    orch._records[session_id] = _record(session_id, tmp_path, model="gpt-5-codex")
    return create_app(orchestrator=orch), orch, client


@pytest.fixture()
def _db_env(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": url}):
        yield url


class TestCodexExportEndpoints:
    def test_export_md_works_for_a_codex_session(self, tmp_path, _db_env):
        """The regression: this used to 404 because transcript_path is None for
        Codex, even though the replay below it is fully engine-neutral."""
        app, _, _ = _codex_app(tmp_path, [_text_turn("t1", "hello codex", "hi human")])
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/codex-1/export.md", headers=_IDENT)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/markdown")
        assert "attachment" in r.headers["content-disposition"]
        assert "hello codex" in r.text and "hi human" in r.text
        assert "gpt-5-codex" in r.text          # sessionMeta model in the header

    def test_export_md_covers_the_full_history(self, tmp_path, _db_env):
        turns = [_text_turn(f"t{i}", f"q-{i}", f"a-{i}") for i in range(40)]
        app, _, _ = _codex_app(tmp_path, turns)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/codex-1/export.md", headers=_IDENT)
        assert r.status_code == 200
        assert "q-0" in r.text and "q-39" in r.text

    def test_export_md_redacts_secrets(self, tmp_path, _db_env):
        """The redaction contract is engine-neutral too: the Codex path goes through
        the same redact_event as Claude's."""
        app, _, _ = _codex_app(
            tmp_path, [_text_turn("t1", "token is sk-livesecrettoken123456", "noted")])
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/codex-1/export.md", headers=_IDENT)
        assert r.status_code == 200
        assert "sk-livesecrettoken123456" not in r.text
        assert "[REDACTED]" in r.text

    def test_export_md_still_404s_with_no_history(self, tmp_path, _db_env):
        app, _, _ = _codex_app(tmp_path, [])
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/unknown-session/export.md", headers=_IDENT)
        assert r.status_code == 404

    def test_export_md_504s_when_replay_times_out(self, tmp_path, _db_env):
        """has_transcript proved history EXISTS, so a None window is a timeout — it
        must never render as a successful export of an empty session."""
        app, orch, _ = _codex_app(tmp_path, [_text_turn("t1", "q", "a")])
        with patch.object(orch, "transcript_window", AsyncMock(return_value=None)), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/codex-1/export.md", headers=_IDENT)
        assert r.status_code == 504
        assert r.json()["detail"]["code"] == "transcript_timeout"

    def test_export_jsonl_gives_codex_declared_normalized_events(self, tmp_path, _db_env):
        app, _, _ = _codex_app(tmp_path, [_text_turn("t1", "hello codex", "hi human")])
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/codex-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/x-ndjson")
        # Named .events.jsonl, not .jsonl: the file is not a Claude transcript and
        # does not get to wear its name.
        assert ".events.jsonl" in r.headers["content-disposition"]

        lines = [json.loads(x) for x in r.text.strip().split("\n")]
        assert lines[0]["type"] == "ccswitchExportMeta"
        assert lines[0]["format"] == NORMALIZED_EXPORT_FORMAT
        assert lines[0]["engine"] == "codex"
        assert lines[0]["sessionId"] == "codex-1"
        # …and it really is the conversation, in the documented wire shape.
        assert any(e.get("type") == "user" and "hello codex" in (e.get("text") or "")
                   for e in lines[1:])
        assert any(e.get("type") == "textDelta" and "hi human" in (e.get("text") or "")
                   for e in lines[1:])

    def test_export_jsonl_404s_with_no_history(self, tmp_path, _db_env):
        app, _, _ = _codex_app(tmp_path, [])
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/unknown-session/export.jsonl", headers=_IDENT)
        assert r.status_code == 404

    def test_export_jsonl_504s_when_replay_times_out(self, tmp_path, _db_env):
        app, orch, _ = _codex_app(tmp_path, [_text_turn("t1", "q", "a")])
        with patch.object(orch, "transcript_window", AsyncMock(return_value=None)), \
             TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/codex-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 504


class TestClaudeExportUnchanged:
    """The Claude arm must be byte-identical to before: a native transcript still
    streams verbatim rather than being re-rendered as normalized events."""

    def _claude_app(self, tmp_path, path: str | None):
        from server.app import create_app
        orch = _orch(tmp_path)
        orch.resolve_transcript_path = AsyncMock(return_value=path)  # type: ignore[method-assign]
        orch.transcript_path = lambda session_id: path  # type: ignore[method-assign]
        # A real-shaped record, not a bare MagicMock: the export route reads `engine`
        # and `model` off it and serialises them, and a MagicMock silently satisfies
        # every getattr — so a double that is "close enough" turns a serialisation
        # contract into an untested one.
        orch.get_session = MagicMock(return_value=SimpleNamespace(  # type: ignore[method-assign]
            id="claude-1", name="Claude Session", title=None,
            engine=AgentEngine.CLAUDE_CODE, model="claude-x"))
        return create_app(orchestrator=orch)

    def test_export_jsonl_streams_the_native_transcript_verbatim(self, tmp_path, _db_env):
        raw = ('{"type":"user","message":{"content":"q"}}\n'
               '{"type":"assistant","message":{"content":[{"type":"text","text":"a"}]}}')
        p = tmp_path / "t.jsonl"
        p.write_text(raw)
        app = self._claude_app(tmp_path, str(p))
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/claude-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 200
        assert r.text == raw                          # byte-for-byte, no header record
        assert "ccswitchExportMeta" not in r.text
        assert r.headers["content-disposition"].endswith('claude-1.jsonl"')

    def test_export_jsonl_now_serves_an_archived_claude_session(self, tmp_path, _db_env):
        """Previously an accepted limitation: transcript_path relies on in-memory
        driver state, so archived Claude sessions 404'd here. resolve_transcript_path
        has the DB-glob fallback, so they stream their real file now."""
        p = tmp_path / "archived.jsonl"
        p.write_text('{"type":"user","message":{"content":"old"}}')
        from server.app import create_app
        orch = _orch(tmp_path)
        orch.resolve_transcript_path = AsyncMock(return_value=str(p))  # type: ignore[method-assign]
        # archived: no in-memory record, so the Claude-only path lookup is blind
        orch.transcript_path = lambda session_id: None  # type: ignore[method-assign]
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/claude-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 200
        assert "old" in r.text

    def test_export_jsonl_404s_when_there_is_no_transcript(self, tmp_path, _db_env):
        app = self._claude_app(tmp_path, None)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/claude-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 404

    def test_export_jsonl_does_not_open_a_foreign_hosts_path(self, tmp_path, _db_env):
        """In fleet mode the resolved path belongs to a WORKER daemon's filesystem.
        Opening it here raises inside the streaming generator — AFTER the response has
        already begun — so the user gets a truncated download rather than an error.

        The contract is therefore about which BRANCH is taken, not about the status
        code: a path this process cannot read must fall through to the (host-independent)
        replay, which in fleet mode is an RPC to the owning daemon and returns the real
        history. Here the replay has nothing behind it, so the body is just the header —
        but the header's presence is the proof that nothing was streamed off a path we
        could not read."""
        app = self._claude_app(tmp_path, "/not/on/this/host/t.jsonl")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/claude-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 200, r.text
        header = json.loads(r.text.strip().split("\n")[0])
        assert header["type"] == "ccswitchExportMeta"
        assert ".events.jsonl" in r.headers["content-disposition"]

    def test_normalized_export_survives_an_unserialisable_record_field(
            self, tmp_path, _db_env):
        """The header is assembled from getattr reads off a session record, and the
        events are whatever the replay produced. One odd value must degrade that field,
        not raise TypeError after a replay that is allowed to run for two minutes and
        turn a finished export into a 500."""
        from server.app import create_app
        orch = _orch(tmp_path)
        orch.resolve_transcript_path = AsyncMock(return_value="/not/here.jsonl")  # type: ignore[method-assign]
        orch.transcript_path = lambda session_id: None  # type: ignore[method-assign]
        orch.get_session = MagicMock(return_value=MagicMock(  # type: ignore[method-assign]
            id="claude-1", name="S", title=None))          # model is a MagicMock
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/claude-1/export.jsonl", headers=_IDENT)
        assert r.status_code == 200, r.text
        assert json.loads(r.text.strip().split("\n")[0])["type"] == "ccswitchExportMeta"


class TestCodexSearchEndpoint:
    def test_search_matches_a_codex_session_on_conversation_content(
            self, tmp_path, _db_env):
        """The #1278 regression, end to end: the session's name does not contain the
        term, so a match can only come from its Codex conversation."""
        app, _, _ = _codex_app(
            tmp_path, [_text_turn("t1", "about the flux capacitor", "indeed")])
        with TestClient(app, raise_server_exceptions=False) as client:
            hit = client.get("/api/sessions/search?q=flux+capacitor", headers=_IDENT)
            miss = client.get("/api/sessions/search?q=nonexistentterm", headers=_IDENT)
        assert hit.status_code == 200, hit.text
        assert hit.json()["ids"] == ["codex-1"]
        assert miss.json()["ids"] == []

    def test_search_does_not_match_on_tool_arguments(self, tmp_path, _db_env):
        """Same selection rule as the Claude extractor: tool JSON is not conversation."""
        app, _, _ = _codex_app(tmp_path, [{
            "id": "t1", "status": "completed", "items": [
                {"type": "userMessage", "id": "t1-u",
                 "content": [{"type": "text", "text": "run it"}]},
                {"type": "commandExecution", "id": "t1-c", "cwd": "/w",
                 "command": "grep uniquetoolmarker .", "status": "completed",
                 "aggregatedOutput": "uniquetoolmarker found"},
            ]}])
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/sessions/search?q=uniquetoolmarker", headers=_IDENT)
        assert r.status_code == 200
        assert r.json()["ids"] == []


# --------------------------------------------------------------------------- #
# transport wiring: a new orchestrator method must cross every boundary          #
# --------------------------------------------------------------------------- #


def test_has_transcript_is_wired_through_every_transport():
    """An orchestrator method an API router calls must exist on the facade, the RPC
    surface, the remote proxy AND the fleet routing table — otherwise it is an
    AttributeError → 500 in deployment behind a fully green in-process suite (#1199).
    """
    from server.sessions.facade import InProcessOrchestratorAdapter
    from server.sessions.fleet_orchestrator import _PER_SESSION
    from server.sessions.remote import RemoteOrchestrator

    assert hasattr(InProcessOrchestratorAdapter, "has_transcript")
    assert hasattr(RemoteOrchestrator, "has_transcript")
    # Per-session: only the daemon that owns the Codex app-server can answer.
    assert "has_transcript" in _PER_SESSION

    src = Path("server/ipc/orchestrator_rpc.py").read_text()
    assert '"has_transcript"' in src


@pytest.mark.asyncio
async def test_remote_orchestrator_has_transcript_proxies_to_the_daemon():
    from server.sessions.remote import RemoteOrchestrator

    class _Client:
        def __init__(self, value):
            self.value, self.last = value, None

        async def call(self, method, **kwargs):
            self.last = (method, kwargs)
            return self.value

    client = _Client(True)
    assert await RemoteOrchestrator(client).has_transcript("s1") is True
    assert client.last == ("has_transcript", {"session_id": "s1"})

    # A daemon that predates this RPC answers None mid-deploy; that must read as the
    # pre-existing 404, not as a truthy object.
    assert await RemoteOrchestrator(_Client(None)).has_transcript("s1") is False
