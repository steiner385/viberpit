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
from server.assistant.tools import (BLOCKED_WHILE_SCOPED, NO_SESSION_TARGET, OPERATOR_TOOLS,
                                    SCOPED_READ_ONLY, SESSION_TARGET_KEYS, TOOL_SPECS,
                                    ToolArgError, ToolClass, build_tools, scope_refusal,
                                    scoped_refusal, session_id_arg, tool_targets)

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


# --------------------------------------------------------------------------- #
# C1: a crafted id must not reach the scoped session by another spelling
# --------------------------------------------------------------------------- #

_CRAFTED = [f"{Y}/../{X}", f"{X}?", f"{X}#", f"{X}/prompt?x=", f"{X}%2F..", f"{X}/",
            f"{X}\x00", f"{X[:-1]}"]


@pytest.mark.parametrize("raw", _CRAFTED + ["", "abc", 7, None])
def test_session_id_arg_rejects_anything_but_a_bare_session_id(raw):
    with pytest.raises(ToolArgError):
        session_id_arg(raw)


def test_session_id_arg_canonicalises_case_and_padding():
    assert session_id_arg(f"  {X.upper()} ") == X


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _CRAFTED)
async def test_a_crafted_single_id_is_refused_while_scoped(raw):
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "send_prompt").handler({"id": raw, "text": "go"})
    assert _refused(out, X) and rec.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _CRAFTED)
async def test_a_crafted_id_in_a_batch_refuses_the_whole_batch_while_scoped(raw):
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    out = await _tool(tools, "archive_sessions").handler(
        {"ids": [Y, raw], "titles": ["other", "crafted"]})
    assert _refused(out, X) and rec.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _CRAFTED)
async def test_a_crafted_id_is_an_argument_error_with_no_scope(raw):
    rec = _Recorder()
    out = await _tool(build_tools(rec.api()), "archive_session").handler({"id": raw})
    assert out["is_error"] is True and "session id" in _text(out)
    assert rec.seen == []


@pytest.mark.asyncio
async def test_an_uppercase_scoped_id_is_the_scoped_session():
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    assert _refused(await _tool(tools, "send_prompt").handler(
        {"id": X.upper(), "text": "go"}), X)
    assert _refused(await _tool(tools, "archive_sessions").handler(
        {"ids": [Y, X.upper()], "titles": ["a", "b"]}), X)
    assert rec.seen == []


@pytest.mark.asyncio
async def test_an_uppercase_scope_still_matches():
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X.upper()))
    assert _refused(await _tool(tools, "send_prompt").handler({"id": X, "text": "go"}), X)
    assert rec.seen == []


@pytest.mark.asyncio
async def test_the_checked_id_is_the_id_that_is_sent():
    """`guarded` hands the handler the canonical id it checked, never the raw one."""
    rec = _Recorder()
    tools = build_tools(rec.api(), current_scope=_Scope(X))
    await _tool(tools, "send_prompt").handler({"id": f" {Y.upper()} ", "text": "go"})
    await _tool(tools, "archive_sessions").handler(
        {"ids": [Y.upper()], "titles": ["a"]})
    assert [(m, p) for m, p, _ in rec.seen] == [
        ("POST", f"/api/sessions/{Y}/prompt"), ("POST", "/api/sessions/batch/archive")]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    # Path arguments OTHER than the session id: a dot-segment in any of them would
    # normalise onto a session route, so the client refuses the path outright.
    ("delete_watch", {"id": f"../../sessions/{X}"}),
    ("delete_notification_rule", {"id": f"../../sessions/{X}"}),
    ("answer_question", {"id": Y, "question_id": f"../../{X}/prompt", "selected": ["a"]}),
    ("resolve_permission", {"id": Y, "perm_id": f"../../{X}/prompt", "decision": "allow"}),
    ("set_automation_enabled", {"id": f"../sessions/{X}", "enabled": False}),
])
async def test_no_path_argument_can_climb_onto_another_route(name, args):
    rec = _Recorder()
    out = await _tool(build_tools(rec.api(), current_scope=_Scope(X)), name).handler(args)
    assert out["is_error"] is True
    assert rec.seen == []


# --------------------------------------------------------------------------- #
# I1: writes whose session target cannot be read off their input
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("trigger_automation_action", {"id": "pr-doctor", "kind": "fix", "item_id": "o/r#1"}),
    ("consolidate_prs", {"repo": "o/r", "numbers": [1, 2]}),
])
async def test_indirect_writes_are_blocked_while_any_session_is_scoped(name, args):
    rec = _Recorder()
    out = await _tool(build_tools(rec.api(), current_scope=_Scope(X)), name).handler(args)
    assert _refused(out, X) and rec.seen == []
    # Unscoped, they run as before.
    rec2 = _Recorder()
    out2 = await _tool(build_tools(rec2.api()), name).handler(args)
    assert out2.get("is_error") is not True, _text(out2)
    assert len(rec2.seen) == 1


# --------------------------------------------------------------------------- #
# M1: one predicate, used before the card and in `guarded`
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,args,scope,refused", [
    ("send_prompt", {"id": X, "text": "t"}, X, True),
    ("mcp__viberpit__send_prompt", {"id": X, "text": "t"}, X, True),
    ("send_prompt", {"id": X.upper(), "text": "t"}, X, True),
    ("send_prompt", {"id": f"{Y}/../{X}", "text": "t"}, X, True),
    ("send_prompt", {"id": Y, "text": "t"}, X, False),
    ("send_prompt", {"id": X, "text": "t"}, None, False),
    ("get_session", {"id": X}, X, False),
    ("trigger_automation_action", {"id": "pr-doctor"}, X, True),
    ("create_session", {"repo": "o/r", "prompt": X}, X, False),
    ("not_a_tool", {"id": X}, X, False),
])
def test_scope_refusal(name, args, scope, refused):
    msg = scope_refusal(name, args, scope)
    assert (msg == scoped_refusal(X)) if refused else msg is None


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
    tables = {"SESSION_TARGET_KEYS": set(SESSION_TARGET_KEYS),
              "NO_SESSION_TARGET": set(NO_SESSION_TARGET),
              "BLOCKED_WHILE_SCOPED": set(BLOCKED_WHILE_SCOPED)}
    seen: dict[str, str] = {}
    for table, names in tables.items():
        for n in names:
            assert n not in seen, f"{n} is in both {seen[n]} and {table}"
            seen[n] = table
    classified = set(seen)
    assert writes == classified, (
        f"unclassified: {sorted(writes - classified)}; "
        f"not write tools: {sorted(classified - writes)}")
    for reasons in (NO_SESSION_TARGET, BLOCKED_WHILE_SCOPED):
        assert all(isinstance(r, str) and r.strip() for r in reasons.values())


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
    ("send_prompt", {"id": X.upper()}, {X}),
    ("send_prompt", {"id": f" {X}\n"}, {X}),
    # A bare string where a list belongs still names its target (the handler rejects
    # the shape; the guard must not let it slip past first).
    ("archive_sessions", {"ids": X}, {X}),
    # A missing target is the handler's to report.
    ("send_prompt", {"text": "t"}, set()),
    ("send_prompt", None, set()),
    ("list_sessions", {"id": "anything"}, set()),
])
def test_tool_targets(name, args, expected):
    assert tool_targets(name, args) == expected


@pytest.mark.parametrize("name,args", [
    ("archive_sessions", {"ids": [Y, f"{X}?"]}),
    ("send_prompt", {"id": 7}),
    ("send_prompt", {"id": f"{Y}/../{X}"}),
])
def test_tool_targets_refuses_a_malformed_target(name, args):
    with pytest.raises(ToolArgError):
        tool_targets(name, args)


# --------------------------------------------------------------------------- #
# The two new READ tools
# --------------------------------------------------------------------------- #

def test_the_diff_description_does_not_overclaim_redaction():
    tools = {t.name: t for t in build_tools(_Recorder().api())}
    desc = tools["get_session_diff"].description
    assert "Credential-shaped strings" in desc and "#3242" in desc
    assert "Secrets in the patch are redacted" not in desc


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
async def test_get_session_diff_full_asks_the_endpoint_for_bounded_patch_text():
    """summary=false is the endpoint's `format=patch` view: it renders, budgets and
    redacts; the tool only chunks each patch for the envelope's per-string cap."""
    patch = "".join(f"+line {i}\n" for i in range(400))
    body = {"files": [{"path": "a.py", "status": "modified", "additions": 400,
                       "deletions": 0, "patch": patch}],
            "additions": 400, "deletions": 0, "truncated": False,
            "cut": {"filesOmitted": 0, "patchesTruncated": 0, "patchesOmitted": 0,
                    "patchBytesOmitted": 0, "maxFiles": 10, "maxPatchBytes": 24000}}
    rec = _Recorder(body)
    out = await _tool(build_tools(rec.api()), "get_session_diff").handler(
        {"session_id": X, "summary": False})
    assert rec.seen == [("GET", f"/api/sessions/{X}/diff",
                         {"format": "patch", "max_files": "10", "max_patch_bytes": "24000"})]
    data = json.loads(_text(out))["data"]
    assert "".join(data["files"][0]["patch"]) == patch
    assert data["cut"] == body["cut"]


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
@pytest.mark.parametrize("limit", ["ten", 2.5, True, [3]])
async def test_search_session_transcript_rejects_a_non_integer_limit(limit):
    rec = _Recorder()
    out = await _tool(build_tools(rec.api()), "search_session_transcript").handler(
        {"session_id": X, "q": "refund", "limit": limit})
    assert out["is_error"] is True and "limit" in _text(out) and rec.seen == []


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


def test_a_seed_applies_only_until_a_context_is_sent():
    """M2: a rebuilt driver inherits its predecessor's scope until its own first
    context — which then wins, even when it names no session."""
    async def run():
        d = _driver("scope2")
        d.seed_context_scope(X)
        assert d.context_scope == X
        await d.start()
        try:
            await d.send_prompt("q", context={"route": "/pipeline"})
            await asyncio.sleep(0.05)
            assert d.context_scope is None
            d.seed_context_scope(Y)  # too late: a context was already sent
            assert d.context_scope is None
        finally:
            await d.aclose()

    asyncio.run(run())


def test_a_prompt_without_context_keeps_the_seed():
    async def run():
        d = _driver("scope3")
        d.seed_context_scope(X)
        await d.start()
        try:
            await d.send_prompt("re-sent after a crash")
            await asyncio.sleep(0.05)
            assert d.context_scope == X
        finally:
            await d.aclose()

    asyncio.run(run())


def test_a_scoped_write_is_denied_before_any_permission_card():
    """M1: the same predicate runs in `_can_use_tool`, so the user is never asked to
    approve a call `guarded` would refuse."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry

    async def run():
        reg = PermissionRegistry()
        d = ClaudeAgentDriver("asst-scope", "primary", "/tmp/r", "/tmp/cfg",
                              mode="interactive", sdk_factory=lambda *a, **k: None,
                              registry=reg, identity="me",
                              assistant_api=_Recorder().api())
        d.seed_context_scope(X)
        events: list = []
        d._emit = events.append  # type: ignore[method-assign]
        denied = await d._can_use_tool("mcp__viberpit__send_prompt", {"id": X, "text": "go"})
        assert isinstance(denied, PermissionResultDeny)
        assert denied.message == f"{SCOPED_READ_ONLY}: {scoped_refusal(X)}"
        crafted = await d._can_use_tool("mcp__viberpit__archive_sessions",
                                        {"ids": [Y, f"{X}#"], "titles": ["a", "b"]})
        assert isinstance(crafted, PermissionResultDeny)
        assert reg.pending("asst-scope") == [] and events == []
        # A read of the scoped session still goes through, card-free.
        ok = await asyncio.wait_for(
            d._can_use_tool("mcp__viberpit__get_session", {"id": X}), 2)
        assert isinstance(ok, PermissionResultAllow)

    asyncio.run(run())


class _FakeDriver:
    def __init__(self, scope: str | None = None) -> None:
        self._scope, self._sent = scope, scope is not None

    @property
    def context_scope(self) -> str | None:
        return self._scope

    def seed_context_scope(self, scope: str | None) -> None:
        if not self._sent:
            self._scope = scope


async def _overlay_scope(orch, monkeypatch, sid: str = "s-asst"):
    import server.assistant.server as server_mod
    captured: dict = {}
    real = server_mod.build_server

    def spy(api, **kw):
        captured.update(kw)
        return real(api, **kw)

    monkeypatch.setattr(server_mod, "build_server", spy)
    from server.sessions.engine import AgentEngine
    await orch._setup_assistant_overlay(sid, identity="u@example.com", model="sonnet",
                                        engine=AgentEngine.CLAUDE_CODE)
    return captured["current_scope"]


def _bare_orch(tmp_path, monkeypatch):
    import time

    import server.db as db_mod
    from server.sessions.orchestrator import SessionOrchestrator
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "cchome"))

    class _Result:
        def scalar_one_or_none(self): return None

    class _Ctx:
        async def __aenter__(self):
            class _Db:
                async def execute(self, *a, **k): return _Result()
            return _Db()

        async def __aexit__(self, *a): return False

    monkeypatch.setattr(db_mod, "db_session", lambda: _Ctx())
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._sdk_factory = None
    orch._x11 = None
    orch._records = {}
    orch._overlays = {}
    orch._drivers = {}
    orch._assistant_scopes = {}
    orch._clock = time.time
    return orch


def _real_orch():
    import os
    from unittest.mock import patch

    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"VIBERPIT_DEV": "1"}):
        return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)


def test_the_scope_memo_is_declared_in_init():
    assert _real_orch()._assistant_scopes == {}


@pytest.mark.asyncio
async def test_a_rotation_rebuild_records_the_outgoing_scope(tmp_path, monkeypatch):
    """Rotation / recompose / crash recovery rebuild the overlay while the old driver
    is still registered: its scope is what the new driver is seeded with."""
    orch = _bare_orch(tmp_path, monkeypatch)
    orch._drivers["s-asst"] = _FakeDriver(X)
    await _overlay_scope(orch, monkeypatch)
    assert orch._assistant_scopes["s-asst"] == X


@pytest.mark.asyncio
async def test_a_reap_records_the_scope_even_with_no_tool_call_and_close_drops_it():
    """The idle reap records the outgoing driver's scope when it tears it down — no
    tool call needed. Closing (the session ends) drops the entry."""
    orch = _real_orch()
    orch._drivers["s-asst"] = _FakeDriver(X)
    await orch._teardown_driver_state("s-asst", keep_assistant_scope=True)
    assert orch._assistant_scopes["s-asst"] == X
    orch._drivers["s-asst"] = _FakeDriver(Y)
    await orch._teardown_driver_state("s-asst")
    assert "s-asst" not in orch._assistant_scopes


def test_the_reap_keeps_the_scope_and_the_other_teardowns_do_not():
    """Pin the call sites: only the assistant idle reap asks teardown to keep it."""
    import inspect

    from server.sessions.orchestrator import SessionOrchestrator
    reap = inspect.getsource(SessionOrchestrator._reap_idle_assistants_once)
    assert "keep_assistant_scope=True" in reap
    for name in ("close_session", "archive_session"):
        src = inspect.getsource(getattr(SessionOrchestrator, name))
        assert "keep_assistant_scope" not in src


def test_a_rebuilt_driver_is_seeded_eagerly_and_its_first_scoped_write_raises_no_card():
    """The seed is applied when the driver is BUILT (`_create_driver`), so the
    pre-card gate sees it on the rebuilt driver's first turn even before any context
    or tool call."""
    from claude_agent_sdk import PermissionResultDeny

    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.engine import AgentEngine
    from server.sessions.permissions import PermissionRegistry

    orch = _real_orch()
    reg = PermissionRegistry()
    built: list = []

    class _Registry:
        def create(self, engine, config):
            d = ClaudeAgentDriver(config.session_id, config.account, config.cwd,
                                  config.config_dir, mode="interactive",
                                  sdk_factory=lambda *a, **k: None, registry=reg,
                                  identity="me", assistant_api=config.assistant_api)
            built.append(d)
            return d

    orch._engine_registry = _Registry()
    orch._assistant_scopes["s-asst"] = X
    d = orch._create_driver(engine=AgentEngine.CLAUDE_CODE, session_id="s-asst",
                            account="a", cwd="/tmp/r", config_dir="/tmp/cfg",
                            mode="interactive", identity="me", resume="r",
                            on_sync_back=lambda: None, assistant_api=_Recorder().api())
    assert d.context_scope == X

    async def run():
        events: list = []
        d._emit = events.append  # type: ignore[method-assign]
        out = await d._can_use_tool("mcp__viberpit__send_prompt", {"id": X, "text": "go"})
        assert isinstance(out, PermissionResultDeny)
        assert out.message.startswith(f"{SCOPED_READ_ONLY}: ")
        assert reg.pending("s-asst") == [] and events == []

    asyncio.run(run())


def test_a_coding_driver_is_never_seeded():
    from server.sessions.engine import AgentEngine
    orch = _real_orch()
    seeded: list = []

    class _D(_FakeDriver):
        def seed_context_scope(self, scope):
            seeded.append(scope)

    class _Registry:
        def create(self, engine, config):
            return _D()

    orch._engine_registry = _Registry()
    orch._assistant_scopes["s-code"] = X
    orch._create_driver(engine=AgentEngine.CLAUDE_CODE, session_id="s-code", account="a",
                        cwd="/tmp/r", config_dir="/tmp/cfg", mode="interactive",
                        identity="me", resume=None, on_sync_back=lambda: None)
    assert seeded == []


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
