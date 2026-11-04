"""D-12: the Assistant drawer as a side chat about the open session.

While the driver's latest context names session X, every MUTATION/DESTRUCTIVE tool
whose target set contains X is refused in `build_tools.guarded` BEFORE its handler —
so before any HTTP call — with `SCOPED_READ_ONLY`. Reads, and writes to OTHER
sessions, are unaffected; with no scope nothing changes.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from server.assistant.client import AssistantApi
from server.assistant.policy import route_allowed
from server.assistant.prompt import ASSISTANT_SYSTEM_PROMPT
from server.assistant.tools import (NO_SESSION_TARGET, OPERATOR_TOOLS, SCOPED_READ_ONLY,
                                    SESSION_TARGET_KEYS, TOOL_SPECS, ToolClass, build_tools,
                                    scoped_refusal, tool_targets)

X = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
Y = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

_WRITE_CLASSES = (ToolClass.MUTATION, ToolClass.DESTRUCTIVE)


class _Recorder:
    """A fake loopback API that records every request it is sent."""

    def __init__(self, body: object = None) -> None:
        self.seen: list[tuple[str, str, dict]] = []
        self._body = {"ok": True} if body is None else body

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.seen.append((req.method, req.url.path, dict(req.url.params)))
        return httpx.Response(200, json=self._body)

    def api(self) -> AssistantApi:
        return AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x",
                            session_id="asst-1", transport=httpx.MockTransport(self.handler))


class _Scope:
    """The late-bound scope source: what the driver's last context named."""

    def __init__(self, sid: str | None) -> None:
        self.sid = sid

    def __call__(self) -> str | None:
        return self.sid


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _text(out: dict) -> str:
    return out["content"][0]["text"]


def _refused(out: dict, sid: str) -> bool:
    return (out.get("is_error") is True and _text(out).startswith(f"{SCOPED_READ_ONLY}: ")
            and scoped_refusal(sid) in _text(out))


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #

def test_the_refusal_message_is_the_constraint_text():
    assert scoped_refusal(X) == (f"This chat is about session {X} and can't change it — "
                                 "ask in the session itself, or close the side chat")


@pytest.mark.asyncio
async def test_send_prompt_to_the_scoped_session_is_refused_with_no_http_call(caplog):
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    with caplog.at_level(logging.INFO, logger="server.assistant.tools"):
        out = await _tool(tools, "send_prompt").handler({"id": X, "text": "continue"})
    assert _refused(out, X)
    assert rec.seen == []
    hits = [r for r in caplog.records if "scoped" in r.getMessage().lower()]
    assert hits and all(r.levelno == logging.INFO for r in hits)


@pytest.mark.asyncio
async def test_a_padded_id_is_still_the_scoped_session():
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "archive_session").handler({"id": f"  {X} "})
    assert _refused(out, X) and rec.seen == []


@pytest.mark.asyncio
async def test_a_batch_containing_the_scoped_session_is_refused_whole():
    """Review Focus 2: never applied partially — the other id is not archived either."""
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "archive_sessions").handler(
        {"ids": [Y, X], "titles": ["other", "this one"]})
    assert _refused(out, X)
    assert rec.seen == []


@pytest.mark.asyncio
async def test_attach_feedback_to_the_scoped_session_is_refused():
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "attach_feedback_to_session").handler(
        {"ids": ["r1"], "session_id": X})
    assert _refused(out, X) and rec.seen == []


@pytest.mark.asyncio
async def test_writes_to_another_session_are_allowed():
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "send_prompt").handler({"id": Y, "text": "continue"})
    assert out.get("is_error") is not True
    assert [(m, p) for m, p, _ in rec.seen] == [("POST", f"/api/sessions/{Y}/prompt")]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("get_session", {"id": X}),
    ("get_session_transcript", {"id": X}),
    ("export_session", {"id": X}),
    ("suggest_next_prompt", {"id": X}),
    ("get_session_diff", {"session_id": X}),
    ("search_session_transcript", {"session_id": X, "q": "refund"}),
])
async def test_reads_of_the_scoped_session_are_allowed(name, args):
    rec = _Recorder({"events": [], "files": [], "hits": [], "suggestions": []})
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, name).handler(args)
    assert out.get("is_error") is not True, _text(out)
    assert len(rec.seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, _Scope(None)])
async def test_with_no_scope_nothing_is_refused(scope):
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=scope)
    out = await _tool(tools, "send_prompt").handler({"id": X, "text": "continue"})
    assert out.get("is_error") is not True
    assert len(rec.seen) == 1


@pytest.mark.asyncio
async def test_the_scope_is_read_at_call_time_when_it_moves_from_a_to_b():
    """Review Focus 1: opened in A, then the user navigates to B. Archiving A is now
    allowed; archiving B is refused."""
    rec = _Recorder()
    scope = _Scope(X)
    tools = build_tools(rec.api(), current_scope=scope)
    archive = _tool(tools, "archive_session")
    assert _refused(await archive.handler({"id": X}), X)
    scope.sid = Y
    assert (await archive.handler({"id": X})).get("is_error") is not True
    assert _refused(await archive.handler({"id": Y}), Y)
    assert [(m, p) for m, p, _ in rec.seen] == [("DELETE", f"/api/sessions/{X}")]


@pytest.mark.asyncio
async def test_tool_input_cannot_set_or_clear_the_scope():
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "send_prompt").handler(
        {"id": X, "text": "go", "scope": None, "current_scope": Y, "context": {}})
    assert _refused(out, X) and rec.seen == []


@pytest.mark.asyncio
async def test_operator_tier_on_the_scope_still_blocks_writes_to_the_scoped_session():
    """Review Focus 5."""
    rec = _Recorder()
    tools = build_tools(rec.api(), operator=True, current_scope=_Scope(X))
    assert OPERATOR_TOOLS <= {t.name for t in tools}
    out = await _tool(tools, "delete_session").handler({"id": X})
    assert _refused(out, X) and rec.seen == []


@pytest.mark.asyncio
async def test_a_refusal_is_metered_and_audited_as_refused(monkeypatch):
    from server.obs.metrics import assistant_tool_calls_total
    captured: dict = {}

    async def fake_record_audit(db, type_, identity, detail, **kw):
        captured["detail"] = detail

    class _Ctx:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False

    import server.audit as audit_mod
    import server.db as db_mod
    monkeypatch.setattr(audit_mod, "record_audit", fake_record_audit)
    monkeypatch.setattr(db_mod, "db_session", lambda: _Ctx())

    before = assistant_tool_calls_total.value("send_prompt", "mutation", "refused")
    tools = build_tools(_Recorder().api(), current_scope=_Scope(X))
    await _tool(tools, "send_prompt").handler({"id": X, "text": "go"})
    assert assistant_tool_calls_total.value("send_prompt", "mutation", "refused") == before + 1
    assert captured["detail"]["ok"] is False
    assert captured["detail"]["refused"] == SCOPED_READ_ONLY


# --------------------------------------------------------------------------- #
# Classification: every write tool is either targeted or explicitly exempt
# --------------------------------------------------------------------------- #

def test_every_write_tool_is_classified_exactly_once():
    """A new MUTATION/DESTRUCTIVE tool — operator tier included — fails here until it
    is placed in SESSION_TARGET_KEYS or NO_SESSION_TARGET (with a reason)."""
    writes = {n for n, s in TOOL_SPECS.items() if s.tool_class in _WRITE_CLASSES}
    assert OPERATOR_TOOLS <= writes
    targeted, exempt = set(SESSION_TARGET_KEYS), set(NO_SESSION_TARGET)
    assert not (targeted & exempt), f"classified twice: {sorted(targeted & exempt)}"
    assert writes == targeted | exempt, (
        f"unclassified: {sorted(writes - targeted - exempt)}; "
        f"not write tools: {sorted((targeted | exempt) - writes)}")
    assert all(isinstance(r, str) and r.strip() for r in NO_SESSION_TARGET.values())


def test_targeted_keys_are_real_schema_properties():
    """A key that is not in the tool's input schema would never match anything."""
    tools = {t.name: t for t in build_tools(_Recorder().api(), operator=True)}
    for name, keys in SESSION_TARGET_KEYS.items():
        schema = tools[name].input_schema
        # Either an explicit JSON schema or the SDK's `{"name": type}` shorthand.
        props = schema.get("properties", schema)
        assert keys and set(keys) <= set(props), (name, keys, sorted(props))


@pytest.mark.parametrize("name,args,expected", [
    ("send_prompt", {"id": X, "text": "t"}, {X}),
    ("archive_sessions", {"ids": [X, Y], "titles": ["a", "b"]}, {X, Y}),
    ("delete_sessions", {"ids": [Y]}, {Y}),
    ("attach_feedback_to_session", {"ids": ["r1", "r2"], "session_id": Y}, {Y}),
    ("create_session", {"repo": "o/r", "prompt": X}, set()),
    ("set_automation_enabled", {"id": X, "enabled": False}, set()),
    # A bare string where a list belongs still names its target (refuse, don't guess);
    # malformed input never raises — the handler's own validation reports it.
    ("archive_sessions", {"ids": X}, {X}),
    ("send_prompt", {"id": 7}, set()),
    ("send_prompt", None, set()),
    ("list_sessions", {"id": X}, set()),
])
def test_tool_targets(name, args, expected):
    assert tool_targets(name, args) == expected


# --------------------------------------------------------------------------- #
# The two new READ tools
# --------------------------------------------------------------------------- #

def test_the_new_tools_are_read_class():
    assert TOOL_SPECS["get_session_diff"].tool_class is ToolClass.READ
    assert TOOL_SPECS["search_session_transcript"].tool_class is ToolClass.READ


@pytest.mark.asyncio
async def test_get_session_diff_reads_the_summary_and_passes_it_through():
    body = {"base": "abc", "head": "def", "additions": 7, "deletions": 2, "truncated": False,
            "files": [{"path": "a.py", "oldPath": None, "status": "modified", "additions": 7,
                       "deletions": 2, "binary": False, "tooLarge": False, "hunks": []}]}
    rec = _Recorder(body)
    out = await _tool(build_tools(rec.api()), "get_session_diff").handler({"session_id": X})
    assert rec.seen == [("GET", f"/api/sessions/{X}/diff", {"summary": "1"})]
    assert route_allowed("GET", f"/api/sessions/{X}/diff")
    data = json.loads(_text(out))["data"]
    assert data["additions"] == 7 and data["deletions"] == 2 and data["base"] == "abc"
    assert data["files"] == [{"path": "a.py", "oldPath": None, "status": "modified",
                              "additions": 7, "deletions": 2, "binary": False,
                              "tooLarge": False}]


@pytest.mark.asyncio
async def test_get_session_diff_caps_the_file_list_and_reports_the_cut():
    files = [{"path": f"f{i}.py", "status": "added", "additions": 1, "deletions": 0,
              "hunks": []} for i in range(250)]
    rec = _Recorder({"files": files, "additions": 250, "deletions": 0, "truncated": False})
    out = await _tool(build_tools(rec.api()), "get_session_diff").handler({"session_id": X})
    env = json.loads(_text(out))
    assert len(env["data"]["files"]) < 250
    assert env["truncated"] == 250 - len(env["data"]["files"])


@pytest.mark.asyncio
async def test_get_session_diff_full_renders_hunks_as_patch_text():
    body = {"files": [{"path": "a.py", "status": "modified", "additions": 1, "deletions": 1,
                       "hunks": [{"oldStart": 3, "oldLines": 1, "newStart": 3, "newLines": 1,
                                  "header": "def f():", "lines": [
                                      {"kind": "del", "text": "    return 1"},
                                      {"kind": "add", "text": "    return 2"},
                                      {"kind": "context", "text": ""}]}]}],
            "additions": 1, "deletions": 1, "truncated": False}
    rec = _Recorder(body)
    out = await _tool(build_tools(rec.api()), "get_session_diff").handler(
        {"session_id": X, "summary": False})
    assert rec.seen == [("GET", f"/api/sessions/{X}/diff", {})]
    f = json.loads(_text(out))["data"]["files"][0]
    assert "hunks" not in f
    patch = "".join(f["patch"])
    assert patch == "@@ -3,1 +3,1 @@ def f():\n-    return 1\n+    return 2\n \n"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,sent", [(None, "20"), (5, "5"), (500, "50"), (-3, "1")])
async def test_search_session_transcript_calls_the_in_session_search(limit, sent):
    body = {"hits": [{"turnId": "t1", "ordinal": 0, "role": "user", "snippet": "refund total",
                      "matchStart": 0, "matchEnd": 6}], "truncated": False, "totalTurns": 9}
    rec = _Recorder(body)
    args: dict = {"session_id": X, "q": "refund"}
    if limit is not None:
        args["limit"] = limit
    out = await _tool(build_tools(rec.api()), "search_session_transcript").handler(args)
    assert rec.seen == [("GET", f"/api/sessions/{X}/search", {"q": "refund", "limit": sent})]
    assert route_allowed("GET", f"/api/sessions/{X}/search")
    assert json.loads(_text(out))["data"] == body


@pytest.mark.asyncio
async def test_search_session_transcript_rejects_an_empty_query_locally():
    rec = _Recorder()
    out = await _tool(build_tools(rec.api()), "search_session_transcript").handler(
        {"session_id": X, "q": "   "})
    assert out["is_error"] is True and rec.seen == []


def test_the_list_search_route_is_not_mistaken_for_a_session_search():
    assert not route_allowed("GET", "/api/sessions/search/search")
    assert not route_allowed("POST", f"/api/sessions/{X}/diff")
    assert not route_allowed("POST", f"/api/sessions/{X}/search")


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

def test_the_prompt_carries_the_side_chat_sentence_and_no_braces():
    assert ("When the context names a session, you are a side chat about it: answer from "
            "its transcript, diff and search, and never act on it.") in ASSISTANT_SYSTEM_PROMPT
    assert "{" not in ASSISTANT_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# Scope source: the driver's last sent context
# --------------------------------------------------------------------------- #

def _driver(sid: str):
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    from server.tests.fakes.fake_sdk import FakeSDKClient

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([], on_can_use_tool=can_use_tool)

    return ClaudeAgentDriver(sid, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=factory, registry=PermissionRegistry(),
                             identity="me")


def test_the_driver_scope_follows_the_last_sent_context():
    async def run():
        d = _driver("scope1")
        assert d.context_scope is None
        await d.start()
        try:
            await d.send_prompt("q1", context={"route": f"/sessions/{X}", "sessionId": X})
            await asyncio.sleep(0.05)
            assert d.context_scope == X
            # The deduped repeat (no header re-sent) keeps the scope.
            await d.send_prompt("q2", context={"route": f"/sessions/{X}", "sessionId": X})
            await asyncio.sleep(0.05)
            assert d.context_scope == X
            # A dedupe reset (compaction, /clear) must not drop the scope with it.
            d._last_context_header = None
            assert d.context_scope == X
            await d.send_prompt("q3", context={"route": f"/sessions/{Y}", "sessionId": Y})
            await asyncio.sleep(0.05)
            assert d.context_scope == Y
            # A context with no session clears it.
            await d.send_prompt("q4", context={"route": "/pipeline"})
            await asyncio.sleep(0.05)
            assert d.context_scope is None
        finally:
            await d.aclose()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_the_overlay_build_wires_the_scope_to_the_live_driver(tmp_path, monkeypatch):
    """The overlay is built BEFORE its driver exists, so the scope has to be read late,
    off whichever driver is registered for the session at call time."""
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "cchome"))

    class _Result:
        def scalar_one_or_none(self): return None

    class _Ctx:
        async def __aenter__(self):
            class _Db:
                async def execute(self, *a, **k): return _Result()
            return _Db()

        async def __aexit__(self, *a): return False

    import server.assistant.server as server_mod
    import server.db as db_mod
    monkeypatch.setattr(db_mod, "db_session", lambda: _Ctx())
    captured: dict = {}
    real = server_mod.build_server

    def spy(api, **kw):
        captured.update(kw)
        return real(api, **kw)

    monkeypatch.setattr(server_mod, "build_server", spy)

    import time

    from server.sessions.engine import AgentEngine
    from server.sessions.orchestrator import SessionOrchestrator

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._sdk_factory = None
    orch._x11 = None
    orch._records = {}
    orch._overlays = {}
    orch._drivers = {}
    orch._clock = time.time
    await orch._setup_assistant_overlay("s-asst", identity="u@example.com", model="sonnet",
                                        engine=AgentEngine.CLAUDE_CODE)
    scope = captured["current_scope"]
    assert scope() is None  # no driver yet

    class _D:
        context_scope = X

    orch._drivers["s-asst"] = _D()
    assert scope() == X
    _D.context_scope = Y
    assert scope() == Y
