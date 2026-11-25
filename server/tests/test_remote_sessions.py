"""Remote sessions (plan 2026-11-25-remote-sessions-reverse): the sessions-API client, the
listing filter, the event translation, the service (poller, watchers, send, answer, kill
switch, degradation), the edge's live view, the daemon RPC round trip, the HTTP routes and
the ``/ws`` attach — all against a fake claude.ai behind ``httpx.MockTransport``.

The live twin is ``test_remote_sessions_live.py`` (``VIBERPIT_TEST_REMOTE_SESSIONS=1``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from server.remote_sessions import listing, service as rs
from server.remote_sessions.client import (KIND_AUTH, KIND_BAD_SHAPE, KIND_HTTP,
                                           KIND_NOT_FOUND, KIND_RATE_LIMITED, KIND_TRANSPORT,
                                           Credentials, SessionsApiClient, UpstreamError)
from server.remote_sessions.translate import RemoteEventTranslator
from server.remote_sessions.view import RESET_CODE, RemoteSessionView, open_in_process

SID = "session_01AbcDEF"
OTHER = "session_02Other"
OWNED = "session_03ViberPit"
CREDS = Credentials(token="sk-secret-token", org_uuid="org-1")


def _iso(seconds_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat() \
        .replace("+00:00", "Z")


def _session(sid: str, **over: Any) -> dict:
    base = {"id": sid, "title": f"title {sid}", "session_status": "running",
            "connection_status": "connected", "environment_kind": "bridge",
            "tags": ["remote-control-repl", "machine-id:m-123"],
            "session_url": f"https://claude.ai/code/{sid}", "updated_at": _iso(),
            "unread": False, "session_context": {"cwd": "/work/repo", "model": "claude-x"}}
    base.update(over)
    return base


# ---------------------------------------------------------------- stream-json fixtures

def _u() -> str:
    return str(uuid.uuid4())


def ev_init() -> dict:
    return {"type": "system", "subtype": "init", "session_id": "cli-1", "model": "claude-x",
            "uuid": _u()}


def ev_prompt(text: str) -> dict:
    return {"type": "user", "uuid": _u(), "session_id": "cli-1", "parent_tool_use_id": None,
            "message": {"role": "user", "content": text}}


def ev_text(text: str, mid: str = "msg_1") -> dict:
    return {"type": "assistant", "uuid": _u(), "session_id": "cli-1",
            "parent_tool_use_id": None,
            "message": {"id": mid, "role": "assistant", "model": "claude-x",
                        "content": [{"type": "text", "text": text}]}}


def ev_tool_use(tool_id: str, name: str = "Bash", tool_input: dict | None = None) -> dict:
    return {"type": "assistant", "uuid": _u(), "session_id": "cli-1",
            "parent_tool_use_id": None,
            "message": {"id": "msg_2", "role": "assistant", "model": "claude-x",
                        "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                     "input": tool_input or {"command": "ls"}}]}}


def ev_can_use_tool(rid: str, tool_id: str, name: str = "Bash",
                    tool_input: dict | None = None) -> dict:
    return {"type": "control_request", "request_id": rid, "uuid": _u(),
            "session_id": SID, "created_at": _iso(),
            "request": {"subtype": "can_use_tool", "tool_name": name,
                        "input": tool_input or {"command": "ls"}, "tool_use_id": tool_id,
                        "display_name": name, "description": "run it"}}


def ev_control_response(rid: str, behavior: str = "allow") -> dict:
    return {"type": "control_response", "uuid": _u(), "session_id": SID,
            "response": {"subtype": "success", "request_id": rid,
                         "response": {"behavior": behavior}}}


def ev_tool_result(tool_id: str) -> dict:
    return {"type": "user", "uuid": _u(), "session_id": "cli-1", "parent_tool_use_id": None,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def ev_result() -> dict:
    return {"type": "result", "subtype": "success", "uuid": _u(), "session_id": "cli-1",
            "duration_ms": 1, "duration_api_ms": 1, "is_error": False, "num_turns": 1,
            "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1},
            "result": "done"}


ASK = {"questions": [
    {"header": "Color", "question": "Which color?", "multiSelect": False,
     "options": [{"label": "red"}, {"label": "blue"}]},
    {"header": "Size", "question": "Which size?", "multiSelect": False,
     "options": [{"label": "S"}, {"label": "L"}]}]}


# ---------------------------------------------------------------- fake claude.ai

class FakeApi:
    """The verified sessions API: list + events with ``after_id`` paging (by uuid),
    POST events. ``status_for`` forces an HTTP status per (method, path)."""

    def __init__(self) -> None:
        self.sessions: list[dict] = [_session(SID), _session(OTHER), _session(OWNED)]
        self.events: dict[str, list[dict]] = {SID: [ev_init(), ev_prompt("hello"),
                                                    ev_text("Hi there")]}
        self.posted: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        self.status_for: dict[tuple[str, str], int] = {}
        self.page_size = 2

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        forced = self.status_for.get((request.method, path))
        if forced is not None:
            return httpx.Response(forced, json={"type": "error"})
        if path == "/v1/sessions" and request.method == "GET":
            return httpx.Response(200, json={"data": self.sessions, "first_id": None,
                                             "last_id": None, "has_more": False})
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["v1", "sessions"] and parts[4] == "events":
            sid = parts[3]
            if sid not in self.events and sid not in {s["id"] for s in self.sessions}:
                return httpx.Response(404, json={"type": "error"})
            if request.method == "POST":
                body = json.loads(request.content)
                for e in body["events"]:
                    self.posted.append((sid, e))
                return httpx.Response(200, json={"events": body["events"]})
            evs = self.events.setdefault(sid, [])
            after = request.url.params.get("after_id")
            start = 0
            if after:
                ids = [e["uuid"] for e in evs]
                start = ids.index(after) + 1 if after in ids else len(evs)
            page = evs[start:start + self.page_size]
            return httpx.Response(200, json={
                "data": page, "first_id": page[0]["uuid"] if page else None,
                "last_id": page[-1]["uuid"] if page else None,
                "has_more": start + self.page_size < len(evs)})
        return httpx.Response(404, json={"type": "error"})


def _service(api: FakeApi, *, enabled: bool = True, owned: tuple = (
        f"https://claude.ai/code/{OWNED}",), creds: Credentials | None = CREDS,
        **kw: Any) -> rs.RemoteSessionsService:
    client = SessionsApiClient(transport=httpx.MockTransport(api.handler))
    flag = {"on": enabled}
    svc = rs.RemoteSessionsService(
        client=client, accounts=lambda: ["acct"], credentials=lambda a: creds,
        owned_urls=lambda: owned, enabled=lambda: flag["on"],
        watch_poll_s=kw.pop("watch_poll_s", 0.02), poll_s=kw.pop("poll_s", 0.05), **kw)
    svc._test_flag = flag  # type: ignore[attr-defined]
    return svc


async def _until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _types(wires: list[dict]) -> list[str]:
    return [w["type"] for w in wires]


# ================================================================= client

@pytest.mark.asyncio
async def test_client_sends_the_verified_headers_and_never_leaks_the_token() -> None:
    api = FakeApi()
    client = SessionsApiClient(transport=httpx.MockTransport(api.handler))
    out = await client.list_sessions(CREDS)
    assert [s["id"] for s in out] == [SID, OTHER, OWNED]
    h = api.requests[0].headers
    assert h["authorization"] == "Bearer sk-secret-token"
    assert h["anthropic-beta"] == "ccr-byoc-2025-07-29"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["x-organization-uuid"] == "org-1"
    assert "sk-secret-token" not in repr(CREDS)
    await client.aclose()


@pytest.mark.asyncio
async def test_client_pages_events_by_uuid_after_id() -> None:
    api = FakeApi()
    api.events[SID] += [ev_result()]
    client = SessionsApiClient(transport=httpx.MockTransport(api.handler))
    page = await client.fetch_events(CREDS, SID)
    assert len(page.data) == 4 and page.last_id == api.events[SID][-1]["uuid"]
    assert not page.has_more
    again = await client.fetch_events(CREDS, SID, after_id=page.last_id)
    assert again.data == [] and again.last_id is None
    afters = [r.url.params.get("after_id") for r in api.requests]
    assert afters[0] is None and afters[1] == api.events[SID][1]["uuid"]
    await client.aclose()


@pytest.mark.parametrize("status,kind", [(401, KIND_AUTH), (403, KIND_AUTH),
                                         (404, KIND_NOT_FOUND), (429, KIND_RATE_LIMITED),
                                         (500, KIND_HTTP), (400, KIND_HTTP)])
@pytest.mark.asyncio
async def test_client_maps_statuses_to_kinds(status: int, kind: str) -> None:
    api = FakeApi()
    api.status_for[("GET", "/v1/sessions")] = status
    client = SessionsApiClient(transport=httpx.MockTransport(api.handler))
    with pytest.raises(UpstreamError) as info:
        await client.list_sessions(CREDS)
    assert info.value.kind == kind and info.value.status == status
    assert "sk-secret-token" not in str(info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_client_bad_shape_and_transport_failures() -> None:
    async def run(handler) -> UpstreamError:
        client = SessionsApiClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(UpstreamError) as info:
                await client.list_sessions(CREDS)
            return info.value
        finally:
            await client.aclose()

    assert (await run(lambda r: httpx.Response(200, json={"sessions": []}))).kind == KIND_BAD_SHAPE
    assert (await run(lambda r: httpx.Response(200, text="<html>"))).kind == KIND_BAD_SHAPE

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)
    err = await run(boom)
    assert err.kind == KIND_TRANSPORT and err.detail == "ConnectError"


@pytest.mark.asyncio
async def test_client_refuses_a_session_id_that_could_leave_the_sessions_path() -> None:
    api = FakeApi()
    client = SessionsApiClient(transport=httpx.MockTransport(api.handler))
    for bad in ("../v1/organizations", "session_x/../../y", ""):
        with pytest.raises(UpstreamError):
            await client.post_events(CREDS, bad, [{}])
        with pytest.raises(UpstreamError):
            await client.fetch_events(CREDS, bad)
    assert api.requests == []
    await client.aclose()


# ================================================================= listing

def test_listing_keeps_live_remote_control_sessions_only() -> None:
    now = time.time()
    show = listing.summarize(_session(SID), "acct", now=now)
    assert show is not None
    assert show == {"id": SID, "account": "acct", "title": f"title {SID}",
                    "status": "running", "connection": "connected", "machineId": "m-123",
                    "origin": "repl", "cwd": "/work/repo", "model": "claude-x",
                    "url": f"https://claude.ai/code/{SID}", "updatedAt": show["updatedAt"],
                    "needsYou": False, "unread": False}
    assert listing.summarize(_session(SID, environment_kind="anthropic_cloud"), "a",
                             now=now) is None
    assert listing.summarize(_session(SID, session_status="archived"), "a", now=now) is None
    recent = listing.summarize(_session(SID, connection_status="disconnected",
                                        updated_at=_iso(60)), "a", now=now)
    assert recent is not None and recent["connection"] == "disconnected"
    assert listing.summarize(_session(SID, connection_status="disconnected",
                                      updated_at=_iso(3600)), "a", now=now) is None
    needs = listing.summarize(_session(SID, session_status="requires_action"), "a", now=now)
    assert needs is not None and needs["needsYou"] is True
    assert listing.summarize({"id": "../x", "environment_kind": "bridge"}, "a", now=now) is None
    assert listing.summarize("garbage", "a", now=now) is None
    odd = listing.summarize(_session(SID, tags="nope", session_context=None, title=7), "a",
                            now=now)
    assert odd is not None and odd["origin"] is None and odd["cwd"] is None and odd["title"] == ""


def test_listing_excludes_viberpit_owned_sessions_by_url_or_id() -> None:
    owned = listing.owned_keys([f"https://claude.ai/code/{SID}", None, ""])
    assert owned == {SID}
    assert listing.summarize(_session(SID), "a", now=time.time(), owned=owned) is None
    assert listing.summarize(_session(OTHER), "a", now=time.time(), owned=owned) is not None


# ================================================================= translation

def test_translation_renders_prompts_text_tools_and_turn_end() -> None:
    tr = RemoteEventTranslator()
    out: list[dict] = []
    for raw in (ev_init(), ev_prompt("hello"), ev_text("Hi there"), ev_tool_use("tu1"),
                ev_tool_result("tu1"), ev_result()):
        out += tr.feed(raw)
    types = _types(out)
    assert "sessionMeta" in types
    user = next(w for w in out if w["type"] == "user")
    assert user["text"] == "hello" and user["remote"] is True
    assert any(w["type"] == "textDelta" and w["text"] == "Hi there" for w in out)
    assert any(w["type"] == "toolCall" and w["toolId"] == "tu1" for w in out)
    assert any(w["type"] == "toolResult" and w["toolId"] == "tu1" for w in out)
    assert "turnEnd" in types
    states = [w["state"] for w in out if w["type"] == "status"]
    assert states[-1] == "idle" and "in_progress" in states
    assert "rateLimit" not in types


def test_translation_turns_can_use_tool_into_a_permission_card_and_resolves_it() -> None:
    tr = RemoteEventTranslator()
    tr.feed(ev_prompt("go"))
    out = tr.feed(ev_can_use_tool("r1", "tu1"))
    card = next(w for w in out if w["type"] == "permissionRequest")
    assert card["permId"] == "r1" and card["tool"] == "Bash" and card["alwaysAllowed"] is False
    assert card["sessionGrantEffective"] is False
    assert out[-1] == {"type": "status", "state": "waiting_input", "count": 0}
    done = tr.feed(ev_control_response("r1", "deny"))
    res = next(w for w in done if w["type"] == "permissionResolved")
    assert res["permId"] == "r1" and res["allowed"] is False and res["via"] == "claude_app"
    assert tr.pending == {}


def test_translation_marks_viberpit_answers_as_ours() -> None:
    tr = RemoteEventTranslator()
    tr.feed(ev_can_use_tool("r1", "tu1"))
    tr.note_answer("r1", "permission", True)
    res = next(w for w in tr.feed(ev_control_response("r1")) if w["type"] == "permissionResolved")
    assert res["allowed"] is True and "via" not in res


def test_translation_settles_an_ask_the_log_shows_answered_another_way() -> None:
    tr = RemoteEventTranslator()
    tr.feed(ev_can_use_tool("r1", "tu1"))
    out = tr.feed({"type": "control_cancel_request", "request_id": "r1", "uuid": _u()})
    assert any(w["type"] == "permissionResolved" and w["allowed"] is None for w in out)
    tr.feed(ev_can_use_tool("r2", "tu2"))
    out = tr.feed(ev_tool_result("tu2"))
    assert any(w["type"] == "permissionResolved" and w["permId"] == "r2" for w in out)
    tr.feed(ev_can_use_tool("r3", "tu3"))
    out = tr.feed(ev_result())
    assert any(w["type"] == "permissionResolved" and w["permId"] == "r3" for w in out)
    assert tr.pending == {}


def test_translation_turns_ask_user_question_into_question_cards() -> None:
    tr = RemoteEventTranslator()
    out = tr.feed(ev_can_use_tool("r9", "tu9", name="AskUserQuestion", tool_input=ASK))
    qs = [w for w in out if w["type"] == "question"]
    assert [q["questionId"] for q in qs] == ["r9:0", "r9:1"]
    assert qs[0]["options"] == [{"label": "red"}, {"label": "blue"}]
    assert not any(w["type"] == "permissionRequest" for w in out)


def test_translation_never_raises_on_garbage() -> None:
    tr = RemoteEventTranslator()
    for raw in (None, 7, {}, {"type": "assistant"}, {"type": "control_request"},
                {"type": "control_response", "response": "x"}, {"type": "brand_new"}):
        assert isinstance(tr.feed(raw), list)


# ================================================================= service

@pytest.mark.asyncio
async def test_kill_switch_off_lists_nothing_and_calls_nothing() -> None:
    api = FakeApi()
    svc = _service(api, enabled=False)
    assert await svc.list_sessions() == {"enabled": False, "sessions": [], "accounts": []}
    for call in (svc.open(SID), svc.send_prompt(SID, "x"), svc.answer(SID, "r1")):
        with pytest.raises(rs.RemoteSessionsError) as info:
            await call
        assert info.value.code == rs.DISABLED
    with pytest.raises(rs.RemoteSessionsError):
        svc.touch(SID)
    assert api.requests == []
    await svc.aclose()


@pytest.mark.asyncio
async def test_list_filters_excludes_owned_and_reports_accounts() -> None:
    api = FakeApi()
    api.sessions.append(_session("session_04Cloud", environment_kind="anthropic_cloud"))
    api.sessions.append(_session("session_05Ask", session_status="requires_action"))
    svc = _service(api)
    out = await svc.list_sessions()
    ids = [s["id"] for s in out["sessions"]]
    assert ids[0] == "session_05Ask"          # needs-you first
    assert set(ids) == {SID, OTHER, "session_05Ask"}
    assert out["enabled"] is True and out["accounts"] == [{"account": "acct", "status": "ok"}]
    await svc.aclose()


@pytest.mark.asyncio
async def test_an_account_without_a_login_is_skipped_not_an_error() -> None:
    api = FakeApi()
    svc = _service(api, creds=None)
    out = await svc.list_sessions()
    assert out["sessions"] == [] and out["accounts"] == [{"account": "acct", "status": "no_login"}]
    assert api.requests == []
    await svc.aclose()


@pytest.mark.asyncio
async def test_list_failures_back_off_and_log_once_per_condition(caplog) -> None:
    api = FakeApi()
    api.status_for[("GET", "/v1/sessions")] = 500
    svc = _service(api, poll_s=0.01)
    with caplog.at_level(logging.WARNING, logger="server.remote_sessions.service"):
        out = await svc.list_sessions()
        assert out["accounts"] == [{"account": "acct", "status": "error:http"}]
        n = len(api.requests)
        await asyncio.sleep(0.1)
        # Backed off: 0.01 * 2**failures, not one call per tick; logged once.
        assert len(api.requests) - n <= 4
    warnings = [r for r in caplog.records if "listing acct failed" in r.getMessage()]
    assert len(warnings) == 1
    del api.status_for[("GET", "/v1/sessions")]
    svc._accounts["acct"].next_at = 0.0
    await svc.refresh()
    assert (await svc.list_sessions())["accounts"][0]["status"] == "ok"
    await svc.aclose()


@pytest.mark.asyncio
async def test_open_snapshots_history_then_streams_new_events_in_process() -> None:
    api = FakeApi()
    svc = _service(api)
    view = await open_in_process(svc, SID)
    assert "textDelta" in _types(view.snapshot) and "user" in _types(view.snapshot)
    assert all("_remote" not in w for w in view.snapshot)
    got: list[dict] = []

    async def consume() -> None:
        async for e in view.events():
            got.append(e.to_wire())

    task = asyncio.create_task(consume())
    api.events[SID] += [ev_text("second", mid="msg_9"), ev_result()]
    await _until(lambda: any(w.get("text") == "second" for w in got))
    await _until(lambda: "turnEnd" in _types(got))
    # The live stream never repeats what the snapshot had.
    assert not any(w.get("text") == "Hi there" for w in got)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert svc._local == {}               # the view released its subscription
    await svc.aclose()


@pytest.mark.asyncio
async def test_open_refuses_unknown_owned_and_bad_ids() -> None:
    api = FakeApi()
    svc = _service(api)
    for sid in ("session_99Nope", OWNED, "../../x"):
        with pytest.raises(rs.RemoteSessionsError) as info:
            await svc.open(sid)
        assert info.value.code == rs.NOT_FOUND
    assert not any("/events" in r.url.path for r in api.requests)
    await svc.aclose()


@pytest.mark.asyncio
async def test_one_watcher_per_session_and_it_stops_without_viewers() -> None:
    api = FakeApi()
    svc = _service(api)
    a, b = await asyncio.gather(svc.open(SID), svc.open(SID))
    assert a["epoch"] == b["epoch"] == 1 and len(svc._watchers) == 1
    svc._watchers[SID].touched_at -= rs.LEASE_S + 1
    await _until(lambda: SID not in svc._watchers)
    with pytest.raises(rs.RemoteSessionsError) as info:
        svc.touch(SID)
    assert info.value.code == rs.NOT_WATCHED
    again = await svc.open(SID)
    assert again["epoch"] == 2 and again["head"] > a["head"]
    await svc.aclose()


@pytest.mark.asyncio
async def test_send_prompt_posts_the_verified_user_event() -> None:
    api = FakeApi()
    svc = _service(api)
    await svc.list_sessions()
    assert await svc.send_prompt(SID, "do it") == {"ok": True, "account": "acct"}
    sid, event = api.posted[-1]
    assert sid == SID
    assert event["type"] == "user" and event["session_id"] == SID
    assert event["parent_tool_use_id"] is None
    assert event["message"] == {"role": "user", "content": "do it"}
    uuid.UUID(event["uuid"])
    for bad in ("", "   ", None, 5):
        with pytest.raises(rs.RemoteSessionsError) as info:
            await svc.send_prompt(SID, bad)
        assert info.value.code == rs.BAD_REQUEST
    await svc.aclose()


@pytest.mark.asyncio
async def test_send_failures_degrade_to_unavailable_or_not_found() -> None:
    api = FakeApi()
    svc = _service(api)
    await svc.list_sessions()
    api.status_for[("POST", f"/v1/sessions/{SID}/events")] = 503
    with pytest.raises(rs.RemoteSessionsError) as info:
        await svc.send_prompt(SID, "x")
    assert info.value.code == rs.UNAVAILABLE and "sk-secret" not in str(info.value)
    api.status_for[("POST", f"/v1/sessions/{SID}/events")] = 404
    with pytest.raises(rs.RemoteSessionsError) as info:
        await svc.send_prompt(SID, "x")
    assert info.value.code == rs.NOT_FOUND
    await svc.aclose()


@pytest.mark.asyncio
async def test_answer_permission_posts_the_verified_control_response() -> None:
    api = FakeApi()
    api.events[SID] += [ev_tool_use("tu1"), ev_can_use_tool("r1", "tu1")]
    svc = _service(api)
    snap = await svc.open(SID)
    assert "permissionRequest" in _types(snap["events"])
    out = await svc.answer(SID, "r1", decision="allow")
    assert out == {"ok": True, "sent": True, "account": "acct", "kind": "permission"}
    _, event = api.posted[-1]
    assert event["type"] == "control_response" and event["session_id"] == SID
    assert event["response"] == {"subtype": "success", "request_id": "r1", "response": {
        "behavior": "allow", "updatedInput": {"command": "ls"}, "tool_name": "Bash"}}
    # Our POST lands in the log; the card resolves as ours.
    api.events[SID].append({"uuid": _u(), "session_id": SID, **event})
    await _until(lambda: any(w["type"] == "permissionResolved" for w in svc._watchers[SID].log))
    res = next(w for w in svc._watchers[SID].log if w["type"] == "permissionResolved")
    assert res["allowed"] is True and "via" not in res
    with pytest.raises(rs.RemoteSessionsError) as info:
        await svc.answer(SID, "r1", decision="allow")
    assert info.value.code == rs.ASK_NOT_PENDING
    await svc.aclose()


@pytest.mark.asyncio
async def test_answer_deny_carries_the_note_or_a_default() -> None:
    api = FakeApi()
    api.events[SID] += [ev_can_use_tool("r1", "tu1"), ev_can_use_tool("r2", "tu2")]
    svc = _service(api)
    await svc.open(SID)
    await svc.answer(SID, "r1", decision="deny", message="  not that  ")
    assert api.posted[-1][1]["response"]["response"] == {"behavior": "deny",
                                                         "message": "not that"}
    await svc.answer(SID, "r2", decision="deny")
    assert api.posted[-1][1]["response"]["response"]["message"] == rs.DENY_MESSAGE
    with pytest.raises(rs.RemoteSessionsError) as info:
        await svc.answer(SID, "r9", decision="allow")
    assert info.value.code == rs.ASK_NOT_PENDING
    await svc.aclose()


@pytest.mark.asyncio
async def test_answer_question_waits_for_every_question_then_sends_once() -> None:
    api = FakeApi()
    api.events[SID] += [ev_can_use_tool("rq", "tuq", name="AskUserQuestion", tool_input=ASK)]
    svc = _service(api)
    await svc.open(SID)
    first = await svc.answer(SID, "rq:0", selected=["red"])
    assert first["sent"] is False and api.posted == []
    with pytest.raises(rs.RemoteSessionsError) as info:
        await svc.answer(SID, "rq:1", selected=[])
    assert info.value.code == rs.BAD_REQUEST
    second = await svc.answer(SID, "rq:1", selected=["my own"], mode="freeform")
    assert second == {"ok": True, "sent": True, "account": "acct", "kind": "question"}
    inner = api.posted[-1][1]["response"]["response"]
    assert inner["behavior"] == "deny"
    assert inner["message"].splitlines() == [
        "[User answered via the question UI]",
        "- Which color? → red",
        "- Which size? → (in their own words) my own"]
    await svc.aclose()


@pytest.mark.asyncio
async def test_answer_without_an_open_watcher_is_refused() -> None:
    svc = _service(FakeApi())
    with pytest.raises(rs.RemoteSessionsError) as info:
        await svc.answer(SID, "r1", decision="allow")
    assert info.value.code == rs.NOT_WATCHED
    await svc.aclose()


@pytest.mark.asyncio
async def test_a_session_that_vanishes_ends_its_watch_with_an_error_frame() -> None:
    api = FakeApi()
    svc = _service(api)
    await svc.open(SID)
    api.status_for[("GET", f"/v1/sessions/{SID}/events")] = 404
    await _until(lambda: SID not in svc._watchers)
    assert api.requests  # and the service is still usable
    assert (await svc.list_sessions())["enabled"] is True
    await svc.aclose()


@pytest.mark.asyncio
async def test_watch_errors_back_off_and_recover(caplog) -> None:
    api = FakeApi()
    svc = _service(api)
    await svc.open(SID)
    api.status_for[("GET", f"/v1/sessions/{SID}/events")] = 500
    with caplog.at_level(logging.WARNING, logger="server.remote_sessions.service"):
        await asyncio.sleep(0.3)
    logged = [r for r in caplog.records if f"reading session {SID}" in r.getMessage()]
    assert len(logged) == 1
    del api.status_for[("GET", f"/v1/sessions/{SID}/events")]
    api.events[SID].append(ev_text("back", mid="msg_b"))
    svc._watchers[SID].touched_at = time.monotonic()
    await _until(lambda: any(w.get("text") == "back" for w in svc._watchers[SID].log),
                 timeout=5.0)
    await svc.aclose()


@pytest.mark.asyncio
async def test_turning_the_kill_switch_off_stops_watchers() -> None:
    api = FakeApi()
    svc = _service(api)
    await svc.open(SID)
    svc._test_flag["on"] = False  # type: ignore[attr-defined]
    await _until(lambda: SID not in svc._watchers)
    await svc.aclose()


def test_codes_and_statuses_agree() -> None:
    assert set(rs.HTTP_STATUS) == set(rs.CODES)
    with pytest.raises(ValueError):
        rs.RemoteSessionsError("NOPE")


# ================================================================= view

@pytest.mark.asyncio
async def test_view_drops_snapshot_overlap_and_resets_on_a_new_epoch() -> None:
    q: asyncio.Queue = asyncio.Queue()

    async def live():
        while True:
            yield await q.get()

    released: list[bool] = []

    async def touch() -> dict:
        return {"epoch": 1}

    view = RemoteSessionView({"epoch": 1, "head": 1, "events": [
        {"type": "user", "_remote": {"epoch": 1, "seq": 0}}]}, live(),
        lambda: released.append(True), touch)
    assert view.snapshot == [{"type": "user"}]
    for item in ({"type": "a", "_remote": {"epoch": 1, "seq": 1}},
                 {"type": "b", "_remote": {"epoch": 1, "seq": 2}},
                 {"type": "c", "_remote": {"epoch": 2, "seq": 0}}):
        q.put_nowait(item)
    got = [e.to_wire() async for e in view.events()]
    assert [w["type"] for w in got] == ["b", "error"]
    assert got[-1]["code"] == RESET_CODE
    assert released == [True]


@pytest.mark.asyncio
async def test_view_resets_when_its_watcher_is_gone() -> None:
    async def live():
        await asyncio.sleep(3600)
        yield {}

    async def touch() -> dict:
        raise rs.RemoteSessionsError(rs.NOT_WATCHED)

    view = RemoteSessionView({"epoch": 1, "head": -1, "events": []}, live(), lambda: None,
                             touch)
    with patch("server.remote_sessions.view.TOUCH_S", 0.01):
        got = [e.to_wire() async for e in view.events()]
    assert [w["code"] for w in got] == [RESET_CODE]


# ================================================================= daemon RPC round trip

@pytest.mark.asyncio
async def test_daemon_rpc_round_trip_streams_over_the_event_bus() -> None:
    from server.ipc.client import DaemonClient
    from server.ipc.daemon import create_daemon_app
    from server.ipc.remote_sessions_rpc import register_remote_sessions
    from server.sessions.remote import RemoteOrchestrator
    from server.tests.ipc_harness import serve_daemon_uds

    api = FakeApi()

    class Orch:
        pass

    orch = Orch()
    orch._remote_sessions = _service(api)  # type: ignore[attr-defined]
    app = create_daemon_app()
    register_remote_sessions(app, orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            listed = await ro.remote_sessions_list()
            assert {s["id"] for s in listed["sessions"]} == {SID, OTHER}
            view = await ro.remote_session_open(SID)
            assert "textDelta" in _types(view.snapshot)
            got: list[dict] = []

            async def consume() -> None:
                async for e in view.events():
                    got.append(e.to_wire())

            task = asyncio.create_task(consume())
            api.events[SID].append(ev_text("over the bus", mid="msg_bus"))
            await _until(lambda: any(w.get("text") == "over the bus" for w in got),
                         timeout=5.0)
            assert all("_remote" not in w for w in got)
            assert await ro.remote_session_prompt(SID, "hi") == {"ok": True, "account": "acct"}
            with pytest.raises(rs.RemoteSessionsError) as info:
                await ro.remote_session_answer(SID, "nope", decision="allow")
            assert info.value.code == rs.ASK_NOT_PENDING
            with pytest.raises(rs.RemoteSessionsError) as info:
                await ro.remote_session_open("session_99Nope")
            assert info.value.code == rs.NOT_FOUND
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            await client.aclose()
            await orch._remote_sessions.aclose()  # type: ignore[attr-defined]


# ================================================================= HTTP + /ws (in process)

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}


def _app(api: FakeApi):
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(poll_interval=9999.0, usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._remote_sessions = _service(api)  # type: ignore[attr-defined]
    return app


def _person():
    """Pass the person-only gate (an attested remote browser) for the request."""
    return patch("server.api.remote_sessions.human_gate_via", return_value=None)


def test_http_routes_list_prompt_answer_and_audit() -> None:
    api = FakeApi()
    api.events[SID] += [ev_can_use_tool("r1", "tu1")]
    with patch.dict(os.environ, _ENV), TestClient(_app(api)) as c, \
            patch("server.api.remote_sessions.record_audit") as audit, _person():
        r = c.get("/api/remote-sessions", headers=_H)
        assert r.status_code == 200 and {s["id"] for s in r.json()["sessions"]} == {SID, OTHER}
        r = c.post(f"/api/remote-sessions/{SID}/prompt", json={"text": "go"}, headers=_H)
        assert r.status_code == 200 and r.json() == {"ok": True, "account": "acct"}
        assert audit.call_args.kwargs["type"] == "remote_session.prompt"
        assert audit.call_args.kwargs["detail"]["outcome"] == "sent"
        assert "go" not in json.dumps(audit.call_args.kwargs["detail"])
        # Not watched yet -> 409; after the /ws open it is answerable.
        r = c.post(f"/api/remote-sessions/{SID}/answer",
                   json={"askId": "r1", "decision": "allow"}, headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["code"] == rs.NOT_WATCHED
        assert audit.call_args.kwargs["detail"]["outcome"] == rs.NOT_WATCHED
        with c.websocket_connect("/ws", headers=_H) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": f"remote:{SID}"}))
            frames = _drain_until(ws, "replayComplete")
            types = _types(frames)
            assert types[0] == "replayReset" and "permissionRequest" in types
            assert all(f.get("sessionId") == f"remote:{SID}" for f in frames)
            r = c.post(f"/api/remote-sessions/{SID}/answer",
                       json={"askId": "r1", "decision": "allow"}, headers=_H)
            assert r.status_code == 200 and r.json()["sent"] is True
            assert audit.call_args.kwargs["type"] == "remote_session.answer"
            ws.send_text(json.dumps({"type": "prompt", "sessionId": f"remote:{SID}",
                                     "text": "no"}))
            err = _drain_until(ws, "error")
            assert any(f.get("code") == "REMOTE_SESSION_FRAME_UNSUPPORTED" for f in err)
            ws.send_text(json.dumps({"type": "detach", "sessionId": f"remote:{SID}"}))
        r = c.post(f"/api/remote-sessions/{SID}/answer", json={"askId": "r1",
                                                               "decision": "maybe"}, headers=_H)
        assert r.status_code == 422


def test_http_send_routes_are_person_only() -> None:
    api = FakeApi()
    with patch.dict(os.environ, _ENV), TestClient(_app(api)) as c, \
            patch("server.api.remote_sessions.human_gate_via", return_value="assistant"):
        r = c.post(f"/api/remote-sessions/{SID}/prompt", json={"text": "go"}, headers=_H)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
        r = c.post(f"/api/remote-sessions/{SID}/answer",
                   json={"askId": "r1", "decision": "allow"}, headers=_H)
        assert r.status_code == 403
    assert api.posted == []


def test_http_requires_identity_and_maps_disabled() -> None:
    api = FakeApi()
    app = _app(api)
    with patch.dict(os.environ, _ENV), TestClient(app) as c, _person():
        assert c.get("/api/remote-sessions").status_code == 401
        app.state.orchestrator._remote_sessions._test_flag["on"] = False
        r = c.get("/api/remote-sessions", headers=_H)
        assert r.status_code == 200 and r.json()["enabled"] is False
        r = c.post(f"/api/remote-sessions/{SID}/prompt", json={"text": "go"}, headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["code"] == rs.DISABLED
        with c.websocket_connect("/ws", headers=_H) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": f"remote:{SID}"}))
            frames = _drain_until(ws, "replayComplete")
            assert any(f.get("code") == rs.DISABLED for f in frames)


def _drain_until(ws, until_type: str, timeout: float = 8.0) -> list[dict]:
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = json.loads(ws.receive_text())
        events.extend(msg.get("events", []) if msg.get("type") == "batch" else [msg])
        if any(e.get("type") == until_type for e in events):
            break
    return events
