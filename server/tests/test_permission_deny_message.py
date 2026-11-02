"""An optional message on a permission DENY reaches the model (D-2, spec §6).

The plan card's "Keep planning" comment and a Desktop-style deny both send
``{"decision": "deny", "message": "…"}``. On Claude the message becomes the
model-visible ``PermissionResultDeny(message=...)`` through the registry's existing
per-perm deny-reason store (the one ``deny_all(reason=...)`` uses). Codex's approval
protocol has no text field, so the deny is applied and the comment is delivered as
the next user prompt instead.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import PermissionResultDeny

from server.sessions import session_actions as sa
from server.sessions.claude_driver import (
    DENY_COMMENT_PROMPT_PREFIX, PROMPT_STEER_DENY_PREFIX, USER_DENY_COMMENT_PREFIX,
    ClaudeAgentDriver,
)
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.orphaned_asks import orphaned_answer_prompt
from server.sessions.permissions import PermissionRegistry
from server.sessions.remote import RemoteOrchestrator
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401

COMMENT = "use sqlite instead"


# ── Claude engine: the comment is the model-visible deny reason ──────────────

class _StuckTurn(FakeSDKClient):
    """First query opens a turn that never closes (the CLI parked in a tool call)."""

    async def query(self, prompt: str):
        self.sent.append(prompt)
        if len(self.sent) == 1:
            self._deliver([f.stream_start(uuid="t1")])
        else:
            self._deliver([f.result(uuid=f"r{len(self.sent)}")])


def _claude(reg: PermissionRegistry) -> ClaudeAgentDriver:
    sdk = _StuckTurn([])

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return sdk

    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=factory, registry=reg, identity="me")


async def _park(d: ClaudeAgentDriver, reg: PermissionRegistry, tool="ExitPlanMode"):
    task = asyncio.create_task(d._can_use_tool(tool, {"plan": "1. do it"}))
    for _ in range(200):
        if reg.pending(d.session_id):
            return task, reg.pending(d.session_id)[0]["permId"]
        await asyncio.sleep(0.005)
    task.cancel()
    raise AssertionError("permission never registered as pending")


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                               usage_fn=lambda name: None)


def _run_claude_deny(message: str | None):
    async def run():
        orch = _orch()
        d = _claude(orch._registry)
        orch._drivers["s1"] = d
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        task, pid = await _park(d, orch._registry)
        kw = {} if message is None else {"message": message}
        ok = await orch.resolve_permission("s1", pid, "deny", "me", **kw)
        result = await asyncio.wait_for(task, timeout=2.0)
        await d.aclose()
        return ok, result, orch

    return asyncio.run(run())


def test_claude_deny_with_message_reaches_the_model():
    ok, result, orch = _run_claude_deny(COMMENT)
    assert ok is True
    assert isinstance(result, PermissionResultDeny)
    # Framed so the model reads the user steering, not a bare "stop" (cf. #1504).
    assert result.message.startswith(USER_DENY_COMMENT_PREFIX)
    assert result.message == f"{USER_DENY_COMMENT_PREFIX}\n{COMMENT}"
    # Read back exactly once — nothing left to attach to a later, unrelated deny.
    assert orch._registry._deny_reasons == {}


def test_claude_deny_without_message_keeps_the_generic_text():
    ok, result, _ = _run_claude_deny(None)
    assert ok is True
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "denied by user"


def test_registry_ignores_a_message_on_allow():
    """Defence in depth below the validator: only a deny records a reason."""
    async def run():
        reg = PermissionRegistry()
        pid, fut, _ = await reg.request("s1", "t", "Bash", {"command": "ls"},
                                        mode="interactive", denylist=set(), identity="me")
        assert reg.resolve(pid, "allow", "me", message=COMMENT) is True
        return await fut, reg._deny_reasons

    allowed, reasons = asyncio.run(run())
    assert allowed is True and reasons == {}


def test_deny_all_and_steer_reasons_are_not_reframed():
    """Only a card comment gets USER_DENY_COMMENT_PREFIX; a deny_all reason (a Stop
    reason, or a prompt steer carrying PROMPT_STEER_DENY_PREFIX) is stored verbatim."""
    steer = f"{PROMPT_STEER_DENY_PREFIX}\nuse sqlite"

    async def run():
        reg = PermissionRegistry()
        out = []
        for reason in ("stopped by user", steer):
            pid, fut, _ = await reg.request("s1", "t", "Bash", {"command": "ls"},
                                            mode="interactive", denylist=set(),
                                            identity="me")
            assert reg.deny_all("s1", reason=reason) == 1
            assert await fut is False
            out.append(reg.take_deny_reason(pid))
        return out

    assert asyncio.run(run()) == ["stopped by user", steer]


# ── Codex: approval protocol carries no text → deny, then a user prompt ──────

class _EngineManagedDriver:
    """Stand-in for CodexAgentDriver's permission seam: it delegates to the shared
    registry exactly as the real driver does (``request_external`` cards)."""

    def __init__(self, reg):
        self._reg = reg

    def resolve_permission(self, perm_id, decision, identity, message=None):
        return self._reg.resolve(perm_id, decision, identity, message=message)


def test_codex_deny_with_message_denies_then_sends_the_comment_as_a_user_prompt():
    async def run():
        orch = _orch()
        orch._drivers["s1"] = _EngineManagedDriver(orch._registry)
        orch.send_prompt = AsyncMock(return_value="sent")  # type: ignore[method-assign]
        pid, fut = await orch._registry.request_external(
            "s1", "t", "Bash", {"command": "rm -rf build"}, identity="me")
        ok = await orch.resolve_permission("s1", pid, "deny", "me", message=COMMENT)
        return ok, await fut, orch

    ok, decision, orch = asyncio.run(run())
    assert ok is True
    assert decision == "deny"                       # the engine still gets its decline
    orch.send_prompt.assert_awaited_once()
    args, kwargs = orch.send_prompt.call_args
    assert args == ("s1", f"{DENY_COMMENT_PROMPT_PREFIX}{COMMENT}")
    # User-sourced: no origin marker, so it renders (and reads to the model) as the
    # operator speaking.
    assert kwargs.get("origin") is None
    assert orch._registry._deny_reasons == {}       # nothing stranded in the store


def test_codex_deny_without_message_sends_nothing():
    async def run():
        orch = _orch()
        orch._drivers["s1"] = _EngineManagedDriver(orch._registry)
        orch.send_prompt = AsyncMock(return_value="sent")  # type: ignore[method-assign]
        pid, fut = await orch._registry.request_external(
            "s1", "t", "Bash", {"command": "ls"}, identity="me")
        assert await orch.resolve_permission("s1", pid, "deny", "me") is True
        return await fut, orch

    decision, orch = asyncio.run(run())
    assert decision == "deny"
    orch.send_prompt.assert_not_awaited()


def test_real_codex_driver_accepts_a_message():
    from server.codex.driver import CodexAgentDriver

    reg = PermissionRegistry()
    drv = CodexAgentDriver.__new__(CodexAgentDriver)
    drv._reg = reg

    async def run():
        pid, fut = await reg.request_external("s1", "t", "Bash", {"command": "ls"},
                                              identity="me")
        assert drv.resolve_permission(pid, "deny", "me", message=COMMENT) is True
        return await fut

    assert asyncio.run(run()) == "deny"


def test_orphaned_deny_carries_the_comment():
    prompt = orphaned_answer_prompt("plan", "deny", comment=COMMENT)
    assert "REJECTED" in prompt and COMMENT in prompt
    assert COMMENT not in orphaned_answer_prompt("plan", "deny")


# ── session_actions validation ───────────────────────────────────────────────

class _FakeOrch:
    def __init__(self, ok=True):
        self.resolve_permission = AsyncMock(return_value=ok)
        self.note_user_interaction = AsyncMock()
        self.pending_permissions = AsyncMock(return_value=[])


def _audit():
    return patch("server.sessions.session_actions.record_audit", new_callable=AsyncMock)


@pytest.mark.asyncio
async def test_deny_message_is_trimmed_and_forwarded():
    orch = _FakeOrch()
    with _audit():
        r = await sa.resolve_permission(orch, object(), session_id="s1", perm_id="p1",
                                        decision="deny", identity="me",
                                        message=f"  {COMMENT}\n")
    assert r == {"ok": True}
    orch.resolve_permission.assert_awaited_once_with("s1", "p1", "deny", "me",
                                                     message=COMMENT)


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [None, "", "   \n\t "])
async def test_blank_message_is_the_same_as_no_message(blank):
    orch = _FakeOrch()
    with _audit():
        await sa.resolve_permission(orch, object(), session_id="s1", perm_id="p1",
                                    decision="deny", identity="me", message=blank)
    # Exactly today's call shape: an older daemon/driver never sees the kwarg.
    orch.resolve_permission.assert_awaited_once_with("s1", "p1", "deny", "me")


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "always"])
async def test_message_on_a_non_deny_is_400(decision):
    orch = _FakeOrch()
    with _audit(), pytest.raises(sa.ActionError) as exc:
        await sa.resolve_permission(orch, object(), session_id="s1", perm_id="p1",
                                    decision=decision, identity="me", message=COMMENT)
    assert (exc.value.code, exc.value.http) == ("MESSAGE_REQUIRES_DENY", 400)
    orch.resolve_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_over_4000_chars_is_400():
    orch = _FakeOrch()
    with _audit(), pytest.raises(sa.ActionError) as exc:
        await sa.resolve_permission(orch, object(), session_id="s1", perm_id="p1",
                                    decision="deny", identity="me", message="x" * 4001)
    assert (exc.value.code, exc.value.http) == ("MESSAGE_TOO_LONG", 400)
    orch.resolve_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_of_exactly_4000_chars_is_accepted():
    orch = _FakeOrch()
    with _audit():
        await sa.resolve_permission(orch, object(), session_id="s1", perm_id="p1",
                                    decision="deny", identity="me", message="x" * 4000)
    orch.resolve_permission.assert_awaited_once()


@pytest.mark.asyncio
async def test_not_pending_with_message_keeps_404():
    orch = _FakeOrch(ok=False)
    with _audit(), pytest.raises(sa.ActionError) as exc:
        await sa.resolve_permission(orch, object(), session_id="s1", perm_id="p1",
                                    decision="deny", identity="me", message=COMMENT)
    assert (exc.value.code, exc.value.http) == ("PERMISSION_NOT_PENDING", 404)


# ── Remote / fleet: forward ``message`` only when set ────────────────────────

class _Client:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        return True


def test_remote_omits_message_when_none_and_forwards_it_when_set():
    client = _Client()
    ro = RemoteOrchestrator.__new__(RemoteOrchestrator)
    ro._client = client

    async def run():
        await ro.resolve_permission("s1", "p1", "deny", "me")
        await ro.resolve_permission("s1", "p1", "deny", "me", message=None)
        await ro.resolve_permission("s1", "p1", "deny", "me", message=COMMENT)

    asyncio.run(run())
    base = {"session_id": "s1", "perm_id": "p1", "decision": "deny", "identity": "me"}
    assert client.calls == [("resolve_permission", base), ("resolve_permission", base),
                            ("resolve_permission", {**base, "message": COMMENT})]


# ── HTTP route ───────────────────────────────────────────────────────────────

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}
_SID = "11111111-1111-4111-8111-111111111111"


class _RouteSessions:
    def __init__(self):
        self.calls: list[tuple] = []

    async def resolve_permission(self, sid, perm_id, decision, identity, **kw):
        self.calls.append((sid, perm_id, decision, identity, kw))
        return True

    async def note_user_interaction(self, sid):
        pass

    async def pending_permissions(self, sid):
        return []


def _client():
    from fastapi.testclient import TestClient

    from server.app import create_app

    with patch.dict(os.environ, _ENV):
        app = create_app(orchestrator=_orch())
    fake = _RouteSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_route_forwards_a_deny_message():
    client, fake = _client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{_SID}/permissions/p1",
                        json={"decision": "deny", "message": COMMENT}, headers=_H)
    assert r.status_code == 200
    assert fake.calls == [(_SID, "p1", "deny", "testuser@example.com",
                           {"message": COMMENT})]


def test_route_rejects_a_message_on_allow():
    client, fake = _client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{_SID}/permissions/p1",
                        json={"decision": "allow", "message": COMMENT}, headers=_H)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "MESSAGE_REQUIRES_DENY"
    assert fake.calls == []


# ── WS frame ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sent, forwarded", [(COMMENT, COMMENT), (None, None), (42, None)])
def test_ws_permission_response_forwards_message(ws_env, sent, forwarded):  # noqa: F811
    _app, orch, client = ws_env([])
    session_id = client.portal.call(orch.create_session, "test-perm-deny-message").id
    seen: list[dict] = []

    async def _fake(_orch, _db, **kw):
        seen.append(kw)
        raise sa.ActionError("PERMISSION_NOT_PENDING", "gone", 404)

    with patch("server.bridge.ws.session_actions.resolve_permission", _fake):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            frame = {"type": "permissionResponse", "sessionId": session_id,
                     "permId": "p1", "decision": "deny"}
            if sent is not None:
                frame["message"] = sent
            ws.send_text(json.dumps(frame))
            _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=4.0)
    client.portal.call(orch.close_session, session_id)
    assert len(seen) == 1 and seen[0]["message"] == forwarded
