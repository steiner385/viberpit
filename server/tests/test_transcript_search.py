"""In-session transcript search (D-11): ``GET /api/sessions/{id}/search``.

The matcher is the list search's (``parse_terms`` + ``terms_match``) and the scope is
``events_text``'s skip rules, shared through ``searchable_event``, minus thinking.
Hits come from the REPLAY (``transcript_window(limit=None)``), never the JSONL, so a
rewound turn is not a hit, and each hit's ordinal is the ``_window`` cursor that
pages its turn in: a user prompt takes the ordinal of the next TurnStart."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import events as ev
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import _PER_SESSION
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.replay import ReplayWindow, _window
from server.sessions.search import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    events_text,
    parse_terms,
    search_events,
)
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds
from server.tests.test_codex_orchestrator import _FakeCodexManager, _FakeThreadClient, _record
from server.tests.test_replay_active_chain import _T

H = {"Tailscale-User-Login": "testuser@example.com"}


def _orch(tmp_path: Path, *, codex=None) -> SessionOrchestrator:
    repo_root = tmp_path / "repos"
    repo_root.mkdir(exist_ok=True)
    return SessionOrchestrator(sdk_factory=lambda *a, **k: None, codex=codex,
                               repo_root=str(repo_root), poll_interval=9999.0)


def _claude(orch: SessionOrchestrator, sid: str, tmp_path: Path) -> None:
    orch._records[sid] = SessionRecord(
        id=sid, name="s", account="alpha", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=f"{sid}-sdk", cols=80, rows=24, cwd=str(tmp_path), status="idle")


def _hits(events, q, limit=SEARCH_DEFAULT_LIMIT):
    return search_events(events, parse_terms(q), limit)


# --- the pure matcher ----------------------------------------------------------------

def test_all_terms_case_insensitive_per_event():
    events = [
        {"type": "user", "turnId": "u0", "text": "Fix the Auth TOKEN refresh"},
        {"type": "turnStart", "turnId": "a0"},
        {"type": "textDelta", "turnId": "a0", "text": "auth only here"},
        {"type": "textDelta", "turnId": "a0", "text": "token and AUTH both"},
    ]
    hits, truncated = _hits(events, "auth token")
    assert truncated is False
    assert [(h["turnId"], h["role"]) for h in hits] == [("u0", "user"), ("a0", "assistant")]
    # The first term's first occurrence is the hit position.
    assert hits[1]["snippet"][hits[1]["matchStart"]:hits[1]["matchEnd"]] == "AUTH"


def test_thinking_tool_json_and_sub_agent_text_are_not_hits():
    events = [
        {"type": "turnStart", "turnId": "a0"},
        {"type": "thinkingDelta", "turnId": "a0", "text": "secret thought"},
        {"type": "toolCall", "turnId": "a0", "name": "Bash", "input": {"command": "secret"}},
        {"type": "toolResult", "toolId": "t", "ok": True, "payload": {"content": "secret"}},
        {"type": "textDelta", "turnId": "s0", "text": "secret", "parentToolUseId": "toolu_1"},
        {"type": "user", "turnId": "s1", "text": "secret", "parentToolUseId": "toolu_1"},
    ]
    assert _hits(events, "secret") == ([], False)
    # The list search keeps indexing thinking: the shared skip rules, not its scope.
    assert "secret thought" in events_text(events)
    assert "toolu" not in events_text(events) and events_text(events).count("secret") == 1


def test_owner_prefix_copies_are_not_hits():
    events = [{"type": "user", "turnId": "u0", "text": "needle", "ownerPrefix": True},
              {"type": "user", "turnId": "u0", "text": "needle"}]
    assert len(_hits(events, "needle")[0]) == 1


def test_snippet_is_80_chars_trimmed_on_word_boundaries():
    text = "alpha " * 30 + "NEEDLE" + " omega" * 30
    hits, _ = _hits([{"type": "user", "turnId": "u0", "text": text}], "needle")
    h = hits[0]
    assert h["snippet"] == ("alpha alpha alpha alpha alpha alpha NEEDLE "
                            "omega omega omega omega omega omega")
    assert (h["matchStart"], h["matchEnd"]) == (36, 42)
    assert len(h["snippet"]) <= 80


def test_short_text_snippet_is_the_whole_text_on_one_line():
    hits, _ = _hits([{"type": "user", "turnId": "u0", "text": "  say\nhello world\n"}],
                    "WORLD")
    h = hits[0]
    assert h["snippet"] == "say hello world"
    assert h["snippet"][h["matchStart"]:h["matchEnd"]] == "world"


def test_match_at_the_very_start_and_end_of_a_long_text():
    head = "needle " + "x" * 200
    tail = "y" * 200 + " needle"
    for text in (head, tail):
        h = _hits([{"type": "user", "turnId": "u", "text": text}], "needle")[0][0]
        assert h["snippet"][h["matchStart"]:h["matchEnd"]] == "needle"
        assert len(h["snippet"]) <= 80


def test_limit_sets_truncated():
    events = [{"type": "user", "turnId": f"u{i}", "text": "needle"} for i in range(5)]
    hits, truncated = _hits(events, "needle", limit=3)
    assert [h["turnId"] for h in hits] == ["u0", "u1", "u2"] and truncated is True
    hits, truncated = _hits(events, "needle", limit=5)
    assert len(hits) == 5 and truncated is False
    assert (SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT) == (200, 1000)


def test_user_prompt_takes_the_next_turnstart_ordinal_assistant_its_own():
    events = [
        {"type": "sessionMeta", "model": "m"},
        {"type": "user", "turnId": "u0", "text": "needle zero"},
        {"type": "turnStart", "turnId": "a0"},
        {"type": "textDelta", "turnId": "a0", "text": "needle a0"},
        {"type": "turnEnd", "turnId": "a0"},
        {"type": "turnStart", "turnId": "a1"},
        # A sub-agent's TurnStart never moves the main-thread ordinal.
        {"type": "turnStart", "turnId": "sub", "parentToolUseId": "toolu"},
        {"type": "textDelta", "turnId": "a1", "text": "needle a1"},
        {"type": "turnEnd", "turnId": "a1"},
        {"type": "user", "turnId": "u2", "text": "needle pending"},
    ]
    hits, _ = _hits(events, "needle")
    assert [(h["turnId"], h["ordinal"]) for h in hits] == [
        ("u0", 0), ("a0", 0), ("a1", 1), ("u2", 2)]


def test_stamped_turn_index_wins_over_counting():
    """Codex RecordedTurns can carry several TurnStarts for one ordinal."""
    events = [
        {"type": "turnStart", "turnId": "c0", "turnIndex": 0},
        {"type": "turnStart", "turnId": "c0b", "turnIndex": 0},
        {"type": "textDelta", "turnId": "c0b", "text": "needle"},
        {"type": "user", "turnId": "u1", "text": "needle"},
        {"type": "turnStart", "turnId": "c1", "turnIndex": 1},
    ]
    assert [h["ordinal"] for h in _hits(events, "needle")[0]] == [0, 1]


# --- ordinals agree with the paging cursor (_window) -----------------------------------

def test_every_hit_is_on_the_window_page_its_ordinal_names(tmp_path):
    t = _T()
    t.turn("1")                       # entries t1 (tool) + a1 (reply): ordinals 0, 1
    t.turn("2", tool=False)           # a2: ordinal 2
    t.turn("3")                       # t3, a3: ordinals 3, 4
    path = t.write(tmp_path)
    full = _window(path, None, None)
    hits, _ = search_events([e.to_wire() for e in full.events], ["prompt"], 50)
    hits += search_events([e.to_wire() for e in full.events], ["reply"], 50)[0]
    assert sorted((h["role"], h["ordinal"]) for h in hits) == [
        ("assistant", 1), ("assistant", 2), ("assistant", 4),
        ("user", 0), ("user", 2), ("user", 3)]
    for h in hits:
        page = _window(path, h["ordinal"] + 1, 1)
        assert page.earliest_turn == h["ordinal"]
        on_page = [e for e in page.events if not getattr(e, "owner_prefix", False)
                   and isinstance(e, (ev.UserMessage, ev.TextDelta))]
        assert any(e.turn_id == h["turnId"] and h["snippet"] in e.text for e in on_page), h


# --- the orchestrator ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_searches_the_replay(tmp_path, isolated_db):
    t = _T()
    t.turn("1")
    t.turn("2")
    path = t.write(tmp_path)
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    with patch.object(orch, "transcript_source", return_value=(path, None)):
        r = await orch.transcript_search("s1", "REPLY", 200)
    assert r["truncated"] is False and r["totalTurns"] == 4
    assert [(h["ordinal"], h["snippet"]) for h in r["hits"]] == [(1, "reply 1"), (3, "reply 2")]


@pytest.mark.asyncio
async def test_rewound_turns_are_not_hits(tmp_path, isolated_db):
    t = _T()
    a1 = t.turn("1")
    t.turn("2")
    t.turn("3")
    t.rewind(a1, cut="u2")
    path = t.write(tmp_path)
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    with patch.object(orch, "transcript_source", return_value=(path, None)):
        r = await orch.transcript_search("s1", "prompt", 200)
        replies = await orch.transcript_search("s1", "reply", 200)
    assert [h["snippet"] for h in r["hits"]] == ["prompt 1"]
    assert [h["snippet"] for h in replies["hits"]] == ["reply 1"]
    assert r["totalTurns"] == 2
    # The file itself still holds the cut turns: a direct read would have found them.
    assert "prompt 3" in Path(path).read_text()


@pytest.mark.asyncio
async def test_codex_session_is_searched_through_its_replay(tmp_path, isolated_db):
    turns = [{"id": f"t{i}", "status": "completed", "items": [
        {"type": "userMessage", "id": f"t{i}-u", "content": [{"type": "text",
                                                              "text": f"question {i}"}]},
        {"type": "agentMessage", "id": f"t{i}-a", "text": f"answer {i} flux"},
    ]} for i in range(3)]
    orch = _orch(tmp_path, codex=_FakeCodexManager(client=_FakeThreadClient(turns)))
    orch._records["s1"] = _record("s1", tmp_path)
    r = await orch.transcript_search("s1", "flux", 200)
    assert [(h["ordinal"], h["role"], h["snippet"]) for h in r["hits"]] == [
        (0, "assistant", "answer 0 flux"), (1, "assistant", "answer 1 flux"),
        (2, "assistant", "answer 2 flux")]
    q = await orch.transcript_search("s1", "question 2", 200)
    assert [(h["ordinal"], h["role"]) for h in q["hits"]] == [(2, "user")]
    assert r["totalTurns"] == 3


@pytest.mark.asyncio
async def test_wire_dict_window_from_a_fake_transcript_window(tmp_path, isolated_db):
    """A remote hop hands back wire dicts with no turnIndex stamp: counting still works."""
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    window = ReplayWindow(events=[
        {"type": "user", "turnId": "u0", "text": "find me"},
        {"type": "turnStart", "turnId": "a0"},
        {"type": "textDelta", "turnId": "a0", "text": "found you"},
        {"type": "turnStart", "turnId": "a1"},
        {"type": "textDelta", "turnId": "a1", "text": "found again"},
    ], earliest_turn=0, total_turns=2)
    fake = AsyncMock(return_value=window)
    with patch.object(orch, "transcript_window", fake):
        r = await orch.transcript_search("s1", "found", 5)
    assert fake.await_args.kwargs["limit"] is None
    assert [(h["turnId"], h["ordinal"]) for h in r["hits"]] == [("a0", 0), ("a1", 1)]


@pytest.mark.asyncio
async def test_limit_is_clamped(tmp_path, isolated_db):
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    window = ReplayWindow(events=[{"type": "user", "turnId": f"u{i}", "text": "x"}
                                  for i in range(SEARCH_MAX_LIMIT + 5)])
    with patch.object(orch, "transcript_window", AsyncMock(return_value=window)):
        r = await orch.transcript_search("s1", "x", 10_000)
    assert len(r["hits"]) == SEARCH_MAX_LIMIT and r["truncated"] is True


@pytest.mark.asyncio
async def test_unknown_session_is_a_keyerror(tmp_path, isolated_db):
    orch = _orch(tmp_path)
    with pytest.raises(KeyError):
        await orch.transcript_search("nope", "x", 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "raises"])
async def test_replay_failure_is_an_empty_result_at_info(tmp_path, isolated_db, caplog, failure):
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    fake = (AsyncMock(return_value=None) if failure == "none"
            else AsyncMock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.INFO), patch.object(orch, "transcript_window", fake):
        r = await orch.transcript_search("s1", "x", 10)
    assert r == {"hits": [], "truncated": False, "totalTurns": 0}
    assert any("transcript_search" in rec.getMessage() and rec.levelno == logging.INFO
               for rec in caplog.records)
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


# --- the daemon RPC, remote proxy and fleet routing -------------------------------------

@pytest.mark.asyncio
async def test_round_trips_over_the_owner_daemon_without_a_warning(tmp_path, isolated_db,
                                                                   caplog):
    t = _T()
    t.turn("1")
    path = t.write(tmp_path)
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    app = create_daemon_app()
    register_orchestrator_rpc(app, orch)
    with caplog.at_level(logging.INFO), \
            patch.object(orch, "transcript_source", return_value=(path, None)):
        async with serve_daemon_uds(app) as sock:
            client = DaemonClient(sock)
            try:
                ro = RemoteOrchestrator(client)
                r = await ro.transcript_search("s1", "reply", 200)
                assert [(h["turnId"], h["ordinal"]) for h in r["hits"]] == [("a1", 1)]
                assert r["totalTurns"] == 2 and r["truncated"] is False
                with pytest.raises(KeyError):
                    await ro.transcript_search("nope", "reply", 200)
            finally:
                await client.aclose()
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert "transcript_search" in _PER_SESSION


class _OldDaemon:
    async def call(self, method, *, timeout=None, **kwargs):
        raise DaemonError("UnknownMethod", method)


@pytest.mark.asyncio
async def test_an_old_daemon_surfaces_its_unknown_method():
    with pytest.raises(DaemonError) as exc:
        await RemoteOrchestrator(_OldDaemon()).transcript_search("s1", "x", 10)
    assert exc.value.err_type == "UnknownMethod"


# --- the route ----------------------------------------------------------------------

def _client(orch):
    from server.app import create_app
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "VIBERPIT_DEV": "1", "VIBERPIT_TAILNET_ALLOWLIST": ""}):
        return TestClient(create_app(orchestrator=orch), raise_server_exceptions=False)


def test_route_returns_hits(tmp_path):
    t = _T()
    t.turn("1")
    path = t.write(tmp_path)
    orch = _orch(tmp_path)
    _claude(orch, "s1", tmp_path)
    client = _client(orch)
    with patch.object(orch, "transcript_source", return_value=(path, None)):
        r = client.get("/api/sessions/s1/search?q=Prompt", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body == {"hits": [{"turnId": "u1", "ordinal": 0, "role": "user",
                              "snippet": "prompt 1", "matchStart": 0, "matchEnd": 6}],
                    "truncated": False, "totalTurns": 2}


@pytest.mark.parametrize("q", ["", "%20%20"])
def test_route_empty_query_is_400(tmp_path, q):
    orch = _orch(tmp_path)
    client = _client(orch)
    with patch.object(orch, "transcript_search", AsyncMock()) as fake:
        r = client.get(f"/api/sessions/s1/search?q={q}", headers=H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "SEARCH_EMPTY"
    fake.assert_not_called()


def test_route_unknown_session_is_404(tmp_path):
    orch = _orch(tmp_path)
    client = _client(orch)
    with patch.object(orch, "transcript_search", AsyncMock(side_effect=KeyError("s1"))):
        assert client.get("/api/sessions/s1/search?q=x", headers=H).status_code == 404


def test_route_old_daemon_is_409(tmp_path):
    orch = _orch(tmp_path)
    client = _client(orch)
    err = DaemonError("UnknownMethod", "transcript_search")
    with patch.object(orch, "transcript_search", AsyncMock(side_effect=err)):
        r = client.get("/api/sessions/s1/search?q=x", headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


def test_route_limit_bounds_and_default(tmp_path):
    orch = _orch(tmp_path)
    client = _client(orch)
    fake = AsyncMock(return_value={"hits": [], "truncated": False, "totalTurns": 0})
    with patch.object(orch, "transcript_search", fake):
        assert client.get("/api/sessions/s1/search?q=x&limit=0", headers=H).status_code == 422
        assert client.get("/api/sessions/s1/search?q=x&limit=1001",
                          headers=H).status_code == 422
        assert client.get("/api/sessions/s1/search?q=x", headers=H).status_code == 200
    assert fake.await_args.args == ("s1", "x", SEARCH_DEFAULT_LIMIT)


def test_route_does_not_collide_with_the_list_search(tmp_path):
    orch = _orch(tmp_path)
    client = _client(orch)
    with patch.object(orch, "list_sessions", lambda: []), \
            patch.object(orch, "transcript_search", AsyncMock()) as per_session:
        r = client.get("/api/sessions/search?q=x", headers=H)
    assert r.status_code == 200 and r.json() == {"ids": []}
    per_session.assert_not_called()


def test_route_requires_identity(tmp_path):
    client = _client(_orch(tmp_path))
    assert client.get("/api/sessions/s1/search?q=x").status_code == 401
