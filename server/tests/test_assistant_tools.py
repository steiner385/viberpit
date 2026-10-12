from __future__ import annotations

import json
from unittest.mock import patch

import httpx

from viberpit.brand import MCP_TOOL_PREFIX as MCP_PREFIX
import jsonschema
import pytest
from mcp.types import ListToolsRequest

from server.assistant.client import (AssistantApi, AssistantApiError,  # noqa: F401
                                     TOOL_CALL_CAPACITY)
from server.assistant.server import build_server
from server.auth.middleware import request_attribution
from server.assistant.tools import (OPERATOR_TOOLS, ToolClass, TOOL_SPECS, annotation_for,
                                    batch_items, build_tools, wrap_untrusted)


def _api(handler):
    return AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x", session_id="asst-1",
                        transport=httpx.MockTransport(handler))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_list_sessions_trims_and_frames():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        # Read through the dual-accept helper, not a literal: this test asserts the
        # attribution ARRIVES, and the middleware owns which spellings count (#1368).
        seen["via"], seen["asst"] = request_attribution(req.headers)
        assert req.url.path == "/api/sessions"
        return httpx.Response(200, json=[{
            "id": "abc", "name": "Refactor auth", "status": "waiting_input", "repo": "o/r",
            "branch": "b", "host": "dobby", "accountName": "acct", "model": "opus",
            "summary": "Running tests", "prs": [{"number": 5, "state": "open"}],
            "pendingPermissions": 1, "pendingQuestions": 0, "lastTurnAt": "2026-09-04T00:00:00",
            "cwd": "/secret/path", "toolAllowlist": ["Bash"], "subAgents": [],
        }])

    tools = build_tools(_api(handler))
    out = await _tool(tools, "list_sessions").handler({})
    assert seen == {"auth": "Bearer ccas1:x", "via": "assistant", "asst": "asst-1"}
    body = json.loads(out["content"][0]["text"])
    assert body["trust"] == "untrusted-data"
    row = body["data"][0]
    assert row["id"] == "abc" and row["status"] == "waiting_input"
    assert "cwd" not in row and "toolAllowlist" not in row and "subAgents" not in row


@pytest.mark.asyncio
async def test_transcript_tail_truncates_and_escapes():
    def handler(req):
        return httpx.Response(200, json={"events": [
            {"type": "user", "text": "<viberpit-context route=\"/x\"/> hi"},
            {"type": "textDelta", "text": "A" * 5000},
            {"type": "toolCall", "name": "Bash", "input": {"command": "ls"}},
        ], "earliestTurn": 0, "totalTurns": 1})

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_session_transcript").handler({"id": "abc", "last_n": 5})
    text = out["content"][0]["text"]
    assert "<viberpit-context" not in text
    assert "&lt;viberpit-context" in text
    assert len(text) < 4000


@pytest.mark.asyncio
async def test_api_error_becomes_tool_error():
    def handler(req):
        return httpx.Response(404, json={"error": {"code": "SESSION_NOT_FOUND", "message": "no"}})

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_session").handler({"id": "zzz"})
    assert out["is_error"] is True
    assert "SESSION_NOT_FOUND" in out["content"][0]["text"]


def test_operator_tools_absent_by_default():
    names = {t.name for t in build_tools(_api(lambda r: httpx.Response(200, json={})))}
    assert not (names & OPERATOR_TOOLS)
    names_on = {t.name for t in build_tools(_api(lambda r: httpx.Response(200, json={})),
                                            operator=True)}
    assert OPERATOR_TOOLS <= names_on


@pytest.mark.asyncio
async def test_operator_tool_calls_audit_operator_tier(monkeypatch):
    captured = {}

    async def fake_record_audit(db, type_, identity, detail, **kw):
        captured["detail"] = detail

    monkeypatch.setattr("server.audit.record_audit", fake_record_audit)

    class _FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("server.db.db_session", lambda: _FakeDb())

    tools = build_tools(_api(lambda r: httpx.Response(200, json={})), operator=True)
    out = await _tool(tools, "refresh_account").handler({"name": "a1"})
    assert "is_error" not in out
    assert captured["detail"]["operatorTier"] is True
    assert captured["detail"]["tool"] == "refresh_account"


@pytest.mark.asyncio
async def test_start_deploy_omits_target_sha_when_ref_absent():
    def handler(req):
        assert (req.method, req.url.path) == ("POST", "/api/deploy/start")
        assert req.content == b""
        return httpx.Response(200, json={"state": "deploying"})

    tools = build_tools(_api(handler), operator=True)
    out = await _tool(tools, "start_deploy").handler({})
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_start_deploy_passes_ref_as_target_sha():
    def handler(req):
        assert (req.method, req.url.path) == ("POST", "/api/deploy/start")
        assert json.loads(req.content) == {"targetSha": "abc123"}
        return httpx.Response(200, json={"state": "deploying"})

    tools = build_tools(_api(handler), operator=True)
    out = await _tool(tools, "start_deploy").handler({"ref": "abc123"})
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_upgrade_host_posts_to_the_named_host():
    def handler(req):
        assert (req.method, req.url.path) == ("POST", "/api/fleet/hosts/rocky/upgrade")
        return httpx.Response(200, json={"ok": True})

    tools = build_tools(_api(handler), operator=True)
    out = await _tool(tools, "upgrade_host").handler({"name": "rocky"})
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_set_account_enabled_patches_the_enabled_flag():
    def handler(req):
        assert (req.method, req.url.path) == ("PATCH", "/api/accounts/a1/enabled")
        assert json.loads(req.content) == {"enabled": False}
        return httpx.Response(200, json={"ok": True})

    tools = build_tools(_api(handler), operator=True)
    out = await _tool(tools, "set_account_enabled").handler({"name": "a1", "enabled": False})
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_start_reauth_posts_to_the_named_account():
    def handler(req):
        assert (req.method, req.url.path) == ("POST", "/api/accounts/a1/reauth")
        return httpx.Response(202, json={"state": "pending"})

    tools = build_tools(_api(handler), operator=True)
    out = await _tool(tools, "start_reauth").handler({"name": "a1"})
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_list_initiatives_filters_status():
    def handler(req):
        assert (req.method, req.url.path) == ("GET", "/api/sdlc/initiatives")
        return httpx.Response(200, json=[{"slug": "a", "status": "build"},
                                         {"slug": "b", "status": "spec"}])

    out = await _tool(build_tools(_api(handler)), "list_initiatives").handler({"status": "spec"})
    assert [i["slug"] for i in json.loads(out["content"][0]["text"])["data"]] == ["b"]


@pytest.mark.asyncio
async def test_get_initiative_fetches_by_slug():
    def handler(req):
        assert (req.method, req.url.path) == ("GET", "/api/sdlc/initiatives/foo-bar")
        return httpx.Response(200, json={"slug": "foo-bar", "stage": "plan"})

    out = await _tool(build_tools(_api(handler)), "get_initiative").handler({"slug": "foo-bar"})
    assert json.loads(out["content"][0]["text"])["data"]["stage"] == "plan"


@pytest.mark.asyncio
async def test_advance_initiative_posts():
    def handler(req):
        assert (req.method, req.url.path) == ("POST", "/api/sdlc/initiatives/a/advance")
        return httpx.Response(200, json={"slug": "a", "status": "plan"})

    out = await _tool(build_tools(_api(handler)), "advance_initiative").handler({"slug": "a"})
    assert json.loads(out["content"][0]["text"])["data"]["status"] == "plan"


@pytest.mark.asyncio
async def test_get_initiative_rejects_bad_slug():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "get_initiative").handler(
        {"slug": "../etc"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected slug must never reach the API"


@pytest.mark.asyncio
async def test_advance_initiative_rejects_bad_slug():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "advance_initiative").handler(
        {"slug": "../etc"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected slug must never reach the API"


def test_specs_and_annotations():
    assert TOOL_SPECS["list_sessions"].tool_class is ToolClass.READ
    assert annotation_for(MCP_PREFIX + "list_sessions").tool_class is ToolClass.READ
    assert annotation_for("mcp__other__list_sessions") is None
    for t in build_tools(_api(lambda r: httpx.Response(200, json={}))):
        assert t.name in TOOL_SPECS, f"tool {t.name} has no ToolSpec"
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is (TOOL_SPECS[t.name].tool_class is ToolClass.READ)


def test_priority_queue_description_names_every_skip_reason():
    from server.pm.queue import SKIP_REASONS

    tool = _tool(build_tools(_api(lambda r: httpx.Response(200, json={}))), "get_priority_queue")
    missing = [r for r in SKIP_REASONS if f"'{r}'" not in tool.description]
    assert not missing, f"get_priority_queue description omits skip reasons: {missing}"


def test_wrap_untrusted_escapes_only_tags():
    s = wrap_untrusted({"t": "a < b and <tag>"})
    assert "a < b" in s and "&lt;tag&gt;" in s


def test_tool_spec_keys_match_web_app_action_titles_fixture():
    """Key-set parity between TOOL_SPECS and web's APP_ACTION_TITLES (Task 2.4,
    spec §4.4). The two maps are hand-maintained in different languages; this
    fixture is the tripwire that catches one side drifting from the other. A
    new/renamed TOOL_SPECS entry must update both this file and
    web/src/session/appActions.ts in the same PR."""
    import json
    from pathlib import Path

    fixture = (Path(__file__).resolve().parents[2]
               / "web/src/session/__fixtures__/appActionToolKeys.json")
    keys = json.loads(fixture.read_text())
    assert set(keys) == set(TOOL_SPECS.keys())


# Tools whose every argument is optional — the SDK's protocol-level input schema for
# each MUST validate `{}`, or the CLI rejects the call (jsonschema.validate runs in
# mcp.server.lowlevel.server.Server.call_tool BEFORE the handler ever sees the args).
_ALL_OPTIONAL_TOOLS = ("list_sessions", "list_prs", "list_accounts", "list_hosts",
                       "list_automations", "get_deploy_status", "list_archived_sessions",
                       "list_notification_channels", "list_notification_rules",
                       "get_quiet_hours", "test_notification", "set_quiet_hours",
                       "list_initiatives", "list_backlog_candidates", "get_backlog_groups",
                       "get_cicd_findings", "get_pr_doctor_consolidation", "list_watches",
                       "list_adoptable_sessions", "get_priority_queue", "list_decisions",
                       "list_holds")


@pytest.mark.asyncio
async def test_server_schemas_reflect_real_required_fields():
    api = _api(lambda r: httpx.Response(200, json={}))
    server_config = build_server(api, operator=True)
    server = server_config["instance"]
    # `_build_schema` (inside claude_agent_sdk.create_sdk_mcp_server) is a private
    # closure with no public accessor; go through the low-level MCP server's own
    # registered `list_tools` handler so we validate the SCHEMA THE CLI ACTUALLY SEES
    # on the wire, not a hand-rolled reimplementation of the SDK's schema-building.
    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list"))
    tools_by_name = {t.name: t.inputSchema for t in result.root.tools}

    assert set(tools_by_name) == set(TOOL_SPECS)

    for name in _ALL_OPTIONAL_TOOLS:
        jsonschema.validate({}, tools_by_name[name])  # must not raise

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({}, tools_by_name["get_session"])


# ---------------------------------------------------------------------------
# Task 3.3 — mutation + destructive tools, rate limit, tool audit
# ---------------------------------------------------------------------------

_SID = "11111111-2222-3333-4444-555555555555"

# (tool name, handler args, expected method, expected path, expected JSON body or None).
# One row per mutation/destructive tool in spec §4.2; the point of the table is that a
# handler is a THIN adapter — arg renaming (`prompt` → `initialPrompt`) and endpoint
# choice are the only behaviour a tool is allowed to have.
_MUTATION_CALLS = [
    ("create_session",
     {"prompt": "fix the flake", "repo": "o/r", "model": "opus",
      "permission_mode": "interactive", "account": "acct"},
     "POST", "/api/sessions",
     {"name": None, "initialPrompt": "fix the flake", "repo": "o/r", "model": "opus",
      "permissionMode": "interactive", "account": "acct", "branch": None,
      "presetId": None, "permissionProfileId": None}),
    ("send_prompt", {"id": _SID, "text": "carry on", "interrupt": True},
     "POST", f"/api/sessions/{_SID}/prompt", {"text": "carry on", "interrupt": True}),
    ("send_prompt", {"id": _SID, "text": "carry on"},
     "POST", f"/api/sessions/{_SID}/prompt", {"text": "carry on", "interrupt": False}),
    ("interrupt_session", {"id": _SID},
     "POST", f"/api/sessions/{_SID}/interrupt", None),
    ("answer_question", {"id": _SID, "question_id": "q1", "selected": ["yes"]},
     "POST", f"/api/sessions/{_SID}/questions/q1", {"selected": ["yes"], "mode": "option"}),
    ("resolve_permission", {"id": _SID, "perm_id": "p1", "decision": "deny"},
     "POST", f"/api/sessions/{_SID}/permissions/p1", {"decision": "deny"}),
    ("set_model", {"id": _SID, "model": "sonnet"},
     "POST", f"/api/sessions/{_SID}/model", {"model": "sonnet"}),
    ("set_permission_mode", {"id": _SID, "mode": "acceptEdits"},
     "POST", f"/api/sessions/{_SID}/permission-mode", {"mode": "acceptEdits"}),
    ("resolve_model_approval", {"id": _SID, "decision": "approve", "model": "opus"},
     "POST", f"/api/sessions/{_SID}/model-approval", {"decision": "approve", "model": "opus"}),
    ("resolve_model_approval", {"id": _SID, "decision": "deny"},
     "POST", f"/api/sessions/{_SID}/model-approval", {"decision": "deny", "model": None}),
    ("archive_session", {"id": _SID}, "DELETE", f"/api/sessions/{_SID}", None),
    ("unarchive_session", {"id": _SID}, "POST", f"/api/sessions/{_SID}/unarchive", None),
    ("set_autopilot_paused", {"id": _SID, "paused": True},
     "POST", f"/api/sessions/{_SID}/autopilot", {"paused": True}),
    ("fix_pr", {"id": _SID, "number": 42},
     "POST", f"/api/sessions/{_SID}/fix-pr", {"number": 42}),
    ("set_automation_enabled", {"id": "autopilot", "enabled": False},
     "PATCH", "/api/automations/autopilot", {"enabled": False}),
    ("rename_session", {"id": _SID, "name": "  Refactor billing  "},
     "PATCH", f"/api/sessions/{_SID}", {"name": "Refactor billing"}),
    ("move_in_queue", {"id": _SID, "op": "top"},
     "POST", f"/api/sessions/{_SID}/queue-move", {"op": "top", "position": None}),
    ("move_in_queue", {"id": _SID, "op": "position", "position": 2},
     "POST", f"/api/sessions/{_SID}/queue-move", {"op": "position", "position": 2}),
    ("pin_account", {"id": _SID, "pinned": True},
     "POST", f"/api/sessions/{_SID}/account-pin", {"pinned": True}),
    ("rotate_session", {"id": _SID}, "POST", f"/api/sessions/{_SID}/rotate", None),
    ("reload_session", {"id": _SID}, "POST", f"/api/sessions/{_SID}/reload", None),
    ("adopt_session", {"claude_session_id": "ext-123"},
     "POST", "/api/sessions/adopt", {"claudeSessionId": "ext-123"}),
    ("advance_initiative", {"slug": "foo-bar"},
     "POST", "/api/sdlc/initiatives/foo-bar/advance", None),
    ("delete_session", {"id": _SID}, "DELETE", f"/api/sessions/{_SID}/permanent", None),
    ("cancel_queued_session", {"id": _SID}, "DELETE", f"/api/sessions/{_SID}/queue", None),
    ("ready_merge_pr", {"repo": "o/r", "number": 7},
     "POST", "/api/pipeline/pr/ready-merge", {"repo": "o/r", "number": 7}),
    ("create_notification_rule",
     {"event_class": "session.done", "action": "channel", "channel_id": "ch1",
      "scope_repo": "o/r", "scope_account": "acct", "scope_automation": "autopilot"},
     "POST", "/api/notifications/rules",
     {"eventClass": "session.done", "action": "channel", "channelId": "ch1",
      "scopeRepo": "o/r", "scopeAccount": "acct", "scopeAutomation": "autopilot"}),
    ("create_notification_rule", {"event_class": "session.done", "action": "push"},
     "POST", "/api/notifications/rules",
     {"eventClass": "session.done", "action": "push", "channelId": None, "scopeRepo": None,
      "scopeAccount": None, "scopeAutomation": None}),
    ("delete_notification_rule", {"id": "rule-1"},
     "DELETE", "/api/notifications/rules/rule-1", None),
    ("set_quiet_hours", {"start": "22:00", "end": "07:00"},
     "PUT", "/api/notifications/quiet-hours",
     {"start": "22:00", "end": "07:00", "timezone": None}),
    ("set_quiet_hours", {"start": "22:00", "end": "07:00", "timezone": "America/New_York"},
     "PUT", "/api/notifications/quiet-hours",
     {"start": "22:00", "end": "07:00", "timezone": "America/New_York"}),
]


@pytest.mark.parametrize("name,args,method,path,body", _MUTATION_CALLS,
                         ids=[f"{r[0]}-{i}" for i, r in enumerate(_MUTATION_CALLS)])
@pytest.mark.asyncio
async def test_mutation_tool_hits_exactly_one_endpoint(name, args, method, path, body):
    seen = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content) if req.content else None
        return httpx.Response(200, json={"id": "new-1"})

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert "is_error" not in out, out
    assert seen["method"] == method
    assert seen["path"] == path
    assert seen["body"] == body


@pytest.mark.asyncio
async def test_create_session_returns_id_and_url():
    def handler(req):
        return httpx.Response(
            201, json={"id": "sess-9", "name": "x", "cwd": "/secret", "status": "idle"})

    out = await _tool(build_tools(_api(handler)), "create_session").handler({"prompt": "go"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == {"id": "sess-9", "url": "/sessions/sess-9", "status": "idle"}


@pytest.mark.asyncio
async def test_create_session_surfaces_queued_status_and_position():
    """#2229: a capacity-saturated request comes back queued, not indistinguishable
    from a started one — the assistant must be able to tell the difference."""
    def handler(req):
        return httpx.Response(201, json={
            "id": "sess-10", "name": "x", "cwd": "/secret",
            "status": "queued", "queuePosition": 3})

    out = await _tool(build_tools(_api(handler)), "create_session").handler({"prompt": "go"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == {"id": "sess-10", "url": "/sessions/sess-10",
                    "status": "queued", "queuePosition": 3}


@pytest.mark.asyncio
async def test_204_mutation_reports_ok():
    def handler(req):
        return httpx.Response(204)

    out = await _tool(build_tools(_api(handler)), "archive_session").handler({"id": _SID})
    assert json.loads(out["content"][0]["text"])["data"] == {"ok": True}


@pytest.mark.asyncio
async def test_resolve_permission_rejects_always_before_any_http_call():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "resolve_permission").handler(
        {"id": _SID, "perm_id": "p1", "decision": "always"})
    assert out["is_error"] is True
    assert "allow" in out["content"][0]["text"] and "deny" in out["content"][0]["text"]
    assert calls == [], "the tool must reject 'always' without touching the API"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
async def test_rename_session_rejects_bad_names_before_any_http_call(name):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "rename_session").handler(
        {"id": _SID, "name": name})
    assert out["is_error"] is True
    assert calls == [], "the tool must reject a bad name without touching the API"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"id": _SID, "op": "sideways"},
    {"id": _SID, "op": "position"},
])
async def test_move_in_queue_rejects_bad_args_before_any_http_call(args):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "move_in_queue").handler(args)
    assert out["is_error"] is True
    assert calls == [], "the tool must reject bad args without touching the API"


@pytest.mark.asyncio
async def test_list_adoptable_sessions_hits_the_read_route():
    def handler(req):
        assert req.method == "GET"
        assert req.url.path == "/api/sessions/adoptable"
        return httpx.Response(200, json={"candidates": [{"claudeSessionId": "ext-1"}]})

    out = await _tool(build_tools(_api(handler)), "list_adoptable_sessions").handler({})
    assert "is_error" not in out
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == {"candidates": [{"claudeSessionId": "ext-1"}]}


@pytest.mark.asyncio
async def test_suggest_next_prompt_is_a_read_shaped_post():
    def handler(req):
        assert req.method == "POST"
        assert req.url.path == f"/api/sessions/{_SID}/suggest"
        return httpx.Response(200, json={"suggestions": ["Add a test for the edge case"]})

    out = await _tool(build_tools(_api(handler)), "suggest_next_prompt").handler({"id": _SID})
    assert "is_error" not in out
    assert TOOL_SPECS["suggest_next_prompt"].tool_class is ToolClass.READ


def test_mutation_and_destructive_annotations():
    specs = TOOL_SPECS
    assert specs["archive_session"].tool_class is ToolClass.MUTATION
    for name in ("delete_session", "cancel_queued_session", "ready_merge_pr"):
        assert specs[name].tool_class is ToolClass.DESTRUCTIVE, name
    for t in build_tools(_api(lambda r: httpx.Response(200, json={})), operator=True):
        spec = TOOL_SPECS[t.name]
        assert t.annotations.destructiveHint is (spec.tool_class is ToolClass.DESTRUCTIVE)
        assert t.annotations.readOnlyHint is (spec.tool_class is ToolClass.READ)
    # Every tool named in spec §4.2 is registered (operator tools only with operator=True).
    assert set(TOOL_SPECS) == {t.name for t in
                               build_tools(_api(lambda r: httpx.Response(200, json={})),
                                          operator=True)}


@pytest.mark.asyncio
async def test_tool_calls_are_rate_limited_per_session():
    api = _api(lambda r: httpx.Response(200, json=[]))
    tool_ = _tool(build_tools(api), "list_sessions")
    for i in range(int(TOOL_CALL_CAPACITY)):
        assert "is_error" not in await tool_.handler({}), f"call {i} should be allowed"
    out = await tool_.handler({})
    assert out["is_error"] is True
    assert "rate limited" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_rate_limit_bucket_is_per_assistant_session():
    """Two assistant sessions must not share one budget."""
    a, b = _api(lambda r: httpx.Response(200, json=[])), _api(
        lambda r: httpx.Response(200, json=[]))
    for _ in range(int(TOOL_CALL_CAPACITY)):
        await _tool(build_tools(a), "list_sessions").handler({})
    assert "is_error" not in await _tool(build_tools(b), "list_sessions").handler({})


@pytest.mark.asyncio
async def test_tool_call_is_audited(tmp_path, monkeypatch):
    from sqlalchemy import select

    from server.db import db_session, init_db
    from server.db.models import AuditEvent

    url = f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}"
    monkeypatch.setenv("VIBERPIT_DB_URL", url)
    await init_db(url)

    api = AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x", session_id="asst-1",
                       identity="u@example.com",
                       transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    out = await _tool(build_tools(api), "archive_session").handler({"id": _SID})
    assert "is_error" not in out

    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "assistant.tool_call"))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.via == "assistant"
    assert row.identity == "u@example.com"
    assert row.detail["tool"] == "archive_session"
    assert row.detail["class"] == "mutation"
    assert row.detail["ok"] is True
    assert isinstance(row.detail["ms"], int)


@pytest.mark.asyncio
async def test_failed_tool_call_is_audited_as_not_ok(tmp_path, monkeypatch):
    from sqlalchemy import select

    from server.db import db_session, init_db
    from server.db.models import AuditEvent

    url = f"sqlite+aiosqlite:///{tmp_path / 'audit_err.db'}"
    monkeypatch.setenv("VIBERPIT_DB_URL", url)
    await init_db(url)

    api = AssistantApi(
        base_url="http://127.0.0.1:1", token="ccas1:x", session_id="asst-1",
        identity="u@example.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(
            404, json={"error": {"code": "SESSION_NOT_FOUND", "message": "no"}})))
    out = await _tool(build_tools(api), "delete_session").handler({"id": _SID})
    assert out["is_error"] is True

    async with db_session() as db:
        row = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "assistant.tool_call"))).scalars().one()
    assert row.detail == {"tool": "delete_session", "class": "destructive",
                          "ok": False, "ms": row.detail["ms"]}


@pytest.mark.asyncio
async def test_audit_failure_never_fails_the_tool(monkeypatch):
    """`db_session()` raises when the DB is not initialised (and can fail transiently
    in prod). A tool call must still return its result."""
    import server.db as db_mod

    monkeypatch.setattr(db_mod, "_session_factory", None)
    out = await _tool(build_tools(_api(lambda r: httpx.Response(204))),
                      "archive_session").handler({"id": _SID})
    assert "is_error" not in out


def test_every_mutation_tool_path_is_in_the_assistant_route_allowlist():
    """The token's route allowlist (server/assistant/policy.py) is enforced by
    middleware BEFORE routing, and these tests drive tools through an httpx
    MockTransport that never reaches it — so a tool whose endpoint was left out of
    the allowlist passes every other test here and 403s in production. This is the
    only place the two lists meet."""
    from server.assistant.policy import route_allowed

    for name, _args, method, path, _body in _MUTATION_CALLS:
        assert route_allowed(method, path), f"{name}: {method} {path} is not allowlisted"


# ---------------------------------------------------------------------------
# Task 8.1 — notification channel / rule / quiet-hours tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_notification_channels_trims_url_to_host_and_drops_headers():
    def handler(req: httpx.Request):
        assert req.url.path == "/api/notifications/channels"
        return httpx.Response(200, json=[{
            "id": "ch1", "name": "Slack", "kind": "webhook",
            "url": "https://hooks.example.com/services/T00/B00/secrettoken?x=1",
            "headers": {"Authorization": "Bearer supersecret"},
            "enabled": True, "createdAt": "2026-09-05T00:00:00",
        }])

    tools = build_tools(_api(handler))
    out = await _tool(tools, "list_notification_channels").handler({})
    row = json.loads(out["content"][0]["text"])["data"][0]
    assert row["url"] == "hooks.example.com"
    assert "headers" not in row
    assert row == {"id": "ch1", "name": "Slack", "kind": "webhook", "url": "hooks.example.com",
                   "enabled": True, "createdAt": "2026-09-05T00:00:00"}


@pytest.mark.asyncio
async def test_list_notification_rules_passes_rows_through():
    rows = [{"id": "r1", "eventClass": "session.done", "action": "push"}]

    def handler(req: httpx.Request):
        assert req.method == "GET" and req.url.path == "/api/notifications/rules"
        return httpx.Response(200, json=rows)

    out = await _tool(build_tools(_api(handler)), "list_notification_rules").handler({})
    assert json.loads(out["content"][0]["text"])["data"] == rows


@pytest.mark.asyncio
async def test_get_quiet_hours_returns_server_shape():
    def handler(req: httpx.Request):
        assert req.method == "GET" and req.url.path == "/api/notifications/quiet-hours"
        return httpx.Response(200, json={"start": "22:00", "end": "07:00"})

    out = await _tool(build_tools(_api(handler)), "get_quiet_hours").handler({})
    assert json.loads(out["content"][0]["text"])["data"] == {"start": "22:00", "end": "07:00"}


@pytest.mark.asyncio
async def test_test_notification_is_read_classed_but_calls_post():
    """`test_notification` is a READ tool (it mutates nothing) whose backing endpoint is a
    POST dry-run — the `READ_POSTS` carve-out (server/assistant/policy.py) is what lets a
    READ tool call POST at all; this proves the carve-out actually applies here."""
    from server.assistant.policy import READ_POSTS

    assert TOOL_SPECS["test_notification"].tool_class is ToolClass.READ
    assert "test_notification" in READ_POSTS

    seen = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content) if req.content else None
        return httpx.Response(200, json={"action": "push", "deliver": True})

    out = await _tool(build_tools(_api(handler)), "test_notification").handler(
        {"event_class": "session.done", "repo": "o/r"})
    assert "is_error" not in out
    assert seen == {"method": "POST", "path": "/api/notifications/test",
                    "body": {"eventClass": "session.done", "repo": "o/r"}}


@pytest.mark.asyncio
async def test_envelope_carries_now_and_truncated():
    """`now` is the model's only clock (B6), and `truncated` tells it a list is partial
    — without which it states a cut-down list as if it were the whole one."""
    tools = build_tools(_api(lambda r: httpx.Response(200, json=[{"id": "a"}] * 3)))
    out = await _tool(tools, "list_archived_sessions").handler({"limit": 1})
    body = json.loads(out["content"][0]["text"])
    assert body["now"].endswith("Z") or "+00:00" in body["now"]
    assert body["truncated"] == 2
    # A handler that dropped nothing must not carry the key at all, so the model
    # never sees `truncated: 0` and wonders what it means.
    out2 = await _tool(tools, "list_hosts").handler({})
    assert "truncated" not in json.loads(out2["content"][0]["text"])


@pytest.mark.asyncio
async def test_report_gap_makes_no_http_call_and_counts():
    from server.obs.metrics import assistant_gap_reports_total

    calls = []
    tools = build_tools(_api(lambda r: calls.append(r) or httpx.Response(500)))
    before = assistant_gap_reports_total.value("deploy")
    out = await _tool(tools, "report_gap").handler({"capability": "deploy",
                                                    "detail": "asked to ship"})
    assert calls == []
    assert out.get("is_error") is not True
    assert assistant_gap_reports_total.value("deploy") == before + 1


@pytest.mark.asyncio
async def test_report_gap_slugifies_the_capability_label():
    """The label is a metric dimension the model supplies freely, so it is coerced to
    a bounded kebab-case slug — an unbounded label set is a cardinality leak."""
    from server.obs.metrics import assistant_gap_reports_total

    tools = build_tools(_api(lambda r: httpx.Response(500)))
    before = assistant_gap_reports_total.value("edit-settings-")
    out = await _tool(tools, "report_gap").handler({"capability": "Edit Settings!"})
    assert json.loads(out["content"][0]["text"])["data"] == {"recorded": "edit-settings-"}
    assert assistant_gap_reports_total.value("edit-settings-") == before + 1


# -- Task 5.3: repo discovery + CI stage models ------------------------------------


@pytest.mark.asyncio
async def test_list_repos_filters_by_substring():
    def h(req):
        assert req.url.path == "/api/github/repos"
        return httpx.Response(200, json=[{"nameWithOwner": "acme/billing"},
                                         {"nameWithOwner": "acme/web"}])

    out = await _tool(build_tools(_api(h)), "list_repos").handler({"query": "bill"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert [r["nameWithOwner"] for r in data] == ["acme/billing"]


@pytest.mark.asyncio
async def test_list_branches_passes_repo_and_returns_the_endpoint_shape():
    """`GET /api/github/branches` answers `{"default":…, "branches":[…]}` (see
    server/api/repos.py) — NOT a bare list. The tool passes it through unchanged so
    the model can see which branch is the default."""
    def h(req):
        assert req.url.path == "/api/github/branches"
        assert req.url.params["repo"] == "acme/billing"
        return httpx.Response(200, json={"default": "main", "branches": ["main", "dev"]})

    out = await _tool(build_tools(_api(h)), "list_branches").handler({"repo": "acme/billing"})
    assert json.loads(out["content"][0]["text"])["data"] == {
        "default": "main", "branches": ["main", "dev"]}


@pytest.mark.asyncio
async def test_list_stage_models_reads_the_pipeline_repos_route():
    def h(req):
        assert req.url.path == "/api/pipeline/repos"
        return httpx.Response(200, json={"acme/billing": {"stages": ["lint", "test"]}})

    out = await _tool(build_tools(_api(h)), "list_stage_models").handler({})
    assert json.loads(out["content"][0]["text"])["data"] == {
        "acme/billing": {"stages": ["lint", "test"]}}


# -- Task 5.4: transcript depth ----------------------------------------------------

# The transcript endpoint serves `ToolCall.to_wire` / `ToolResult.to_wire` verbatim
# (server/sessions/events.py): the id key is `toolId`, a toolResult carries `ok` and
# `payload`, and it has NO turnId. These fixtures copy that shape exactly.


@pytest.mark.asyncio
async def test_transcript_tool_rows_carry_ids():
    def h(req):
        return httpx.Response(200, json={"events": [
            {"type": "toolCall", "turnId": "t1", "toolId": "c1", "name": "Bash",
             "toolKind": "bash", "input": {"command": "pytest"}},
        ], "totalTurns": 1})

    out = await _tool(build_tools(_api(h)), "get_session_transcript").handler({"id": _SID})
    row = json.loads(out["content"][0]["text"])["data"]["events"][0]
    assert row == {"tool": "Bash", "turnId": "t1", "toolId": "c1"}


@pytest.mark.asyncio
async def test_get_tool_result_selects_one_and_caps():
    def h(req):
        assert req.url.path.endswith("/transcript")
        return httpx.Response(200, json={"events": [
            {"type": "toolCall", "turnId": "t1", "toolId": "c1", "name": "Bash",
             "input": {"command": "pytest -x"}},
            {"type": "toolResult", "toolId": "c1", "ok": False,
             "payload": {"content": "F" * 9000}},
            {"type": "toolResult", "toolId": "c2", "ok": True,
             "payload": {"content": "other"}},
        ]})

    out = await _tool(build_tools(_api(h)), "get_tool_result").handler(
        {"id": _SID, "tool_id": "c1"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["tool"] == "Bash" and data["input"] == {"command": "pytest -x"}
    assert data["ok"] is False
    assert len(data["output"]) <= 4001 and data["output"].endswith("…")
    assert "other" not in data["output"]


@pytest.mark.asyncio
async def test_get_tool_result_renders_block_list_content():
    """The SDK's tool_result content is a string for some tools and a list of content
    blocks for others; both must render."""
    def h(req):
        return httpx.Response(200, json={"events": [
            {"type": "toolResult", "toolId": "c1", "ok": True,
             "payload": {"content": [{"type": "text", "text": "line one"},
                                     {"type": "text", "text": "line two"}]}},
        ]})

    out = await _tool(build_tools(_api(h)), "get_tool_result").handler(
        {"id": _SID, "tool_id": "c1"})
    assert json.loads(out["content"][0]["text"])["data"]["output"] == "line one\nline two"


@pytest.mark.asyncio
async def test_get_tool_result_unknown_id_is_a_local_arg_error():
    def h(req):
        return httpx.Response(200, json={"events": []})

    out = await _tool(build_tools(_api(h)), "get_tool_result").handler(
        {"id": _SID, "tool_id": "nope"})
    assert out["is_error"] is True
    assert "toolId" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_export_session_returns_tail_text():
    def h(req):
        assert req.url.path.endswith("/export.md")
        return httpx.Response(200, text="# head\n" + "x" * 30000 + "\nTAIL")

    out = await _tool(build_tools(_api(h)), "export_session").handler(
        {"id": _SID, "max_chars": 100})
    data = json.loads(out["content"][0]["text"])["data"]
    # 100 is below the 500 floor, so the cap clamps UP to 500 — the tail is what matters.
    assert data["markdown"].endswith("TAIL")
    assert len(data["markdown"]) <= 500 and data["truncatedChars"] > 0


# --- PM product-layer tools (plan Task 6.2, Pack P) ---------------------------------


@pytest.mark.asyncio
async def test_get_product_deps_asks_for_the_summary_by_default():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"edges": {"total": 1}})

    tools = build_tools(_api(handler))
    await _tool(tools, "get_product_deps").handler({"slug": "kindash"})
    assert seen["params"] == {"summary": "true", "active": "true", "open_only": "true"}
    await _tool(tools, "get_product_deps").handler({"slug": "kindash", "summary": False})
    assert seen["params"] == {"active": "true", "open_only": "true"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,method,path", [
    ("list_products", {}, "GET", "/api/pm/projects"),
    ("get_product", {"slug": "kindash"}, "GET", "/api/pm/projects/kindash"),
    ("get_product_deps", {"slug": "kindash"}, "GET", "/api/pm/projects/kindash/deps"),
    ("get_onboarding_status", {"slug": "kindash"}, "GET",
     "/api/pm/projects/kindash/onboarding"),
    ("list_triage", {}, "GET", "/api/pm/triage"),
    ("request_product_sync", {"slug": "kindash"}, "POST", "/api/pm/projects/kindash/sync"),
])
async def test_product_tools_hit_one_endpoint(name, args, method, path):
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"ok": True})

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert "is_error" not in out
    assert seen == [(method, path)]


@pytest.mark.asyncio
async def test_list_triage_asks_for_pending_only():
    """GET /api/pm/triage is WIDENED for the inbox: it also carries the loop's
    fresh auto-accepts for their undo window, so the assistant would report them
    as "awaiting a decision". The tool asks for the un-widened list."""
    seen: list[str] = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        return httpx.Response(200, json=[])

    out = await _tool(build_tools(_api(handler)), "list_triage").handler({})
    assert "is_error" not in out
    assert len(seen) == 1 and "pendingOnly=true" in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_product", "get_product_deps", "get_onboarding_status",
                                  "request_product_sync"])
async def test_product_tools_reject_a_bad_slug_locally(name):
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), name).handler({"slug": "../etc"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected slug must never reach the API"


@pytest.mark.asyncio
async def test_get_priority_queue_forwards_min_status_and_caps_entries():
    seen = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={
            "entries": [{"ref": f"o/r#{i}", "score": 100 - i} for i in range(60)],
            "skipped": {"blocked": 4, "status": 9},
            "generatedAt": "2026-09-05T00:00:00+00:00",
        })

    out = await _tool(build_tools(_api(handler)), "get_priority_queue").handler(
        {"min_status": "backlog"})
    # The route takes the board's spelling; lowercase used to 422 there (#2979).
    assert seen == {"method": "GET", "path": "/api/pm/queue",
                    "params": {"min_status": "Backlog"}}
    body = json.loads(out["content"][0]["text"])
    # Capped, but the envelope still reports the drop, so the model never states a
    # truncated queue as if it were the whole one.
    assert len(body["data"]["entries"]) == 50
    assert body["truncated"] == 10
    # `skipped` (reason -> count) is what answers "what is blocked by a dependency?",
    # so it must survive the trim untouched.
    assert body["data"]["skipped"] == {"blocked": 4, "status": 9}


@pytest.mark.asyncio
async def test_get_priority_queue_lists_the_items_behind_a_reason():
    """#2979: ``reason`` forwards to the route, whose ``skippedItems`` is narrowed by
    ``product`` like everything else; an unknown reason or status never leaves."""
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={
            "entries": [], "skipped": {"blocked": 2},
            "skippedByProduct": {"kindash": {"blocked": 1}, "viberpit": {"blocked": 1}},
            "skippedItems": [{"ref": "o/k#1", "product": "kindash", "reason": "blocked"},
                             {"ref": "o/r#2", "product": "viberpit", "reason": "blocked"}]})

    tool = _tool(build_tools(_api(handler)), "get_priority_queue")
    out = await tool.handler({"reason": "blocked", "product": "viberpit", "min_status": "Ready"})
    assert seen["params"] == {"reason": "blocked", "min_status": "Ready"}
    body = json.loads(out["content"][0]["text"])
    assert [i["ref"] for i in body["data"]["skippedItems"]] == ["o/r#2"]

    seen.clear()
    for bad in ({"reason": "nope"}, {"min_status": "done"}):
        res = await tool.handler(bad)
        assert res.get("is_error") and seen == {}


@pytest.mark.asyncio
async def test_get_priority_queue_omits_min_status_when_unset():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"entries": [], "skipped": {}})

    await _tool(build_tools(_api(handler)), "get_priority_queue").handler({})
    assert seen["params"] == {}, "an unset min_status must let the server default apply"


@pytest.mark.asyncio
async def test_get_priority_queue_narrows_to_one_product_and_keeps_the_breakdown():
    """Spec 2026-09-30 §6: ``product`` narrows entries AND the per-product maps in
    the handler (one endpoint, presentation-only filtering, like list_prs' repo)."""
    def handler(req: httpx.Request):
        return httpx.Response(200, json={
            "entries": [{"ref": "o/k#1", "product": "kindash", "score": 3},
                        {"ref": "o/r#2", "product": "viberpit", "score": 2}],
            "skipped": {"held": 5, "blocked": 1},
            "skippedByProduct": {"kindash": {"held": 4}, "viberpit": {"held": 1, "blocked": 1}},
            "dispatchableByProduct": {"kindash": 1, "viberpit": 1},
            "generatedAt": "2026-09-05T00:00:00+00:00",
        })

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_priority_queue").handler({"product": "kindash"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert [e["ref"] for e in data["entries"]] == ["o/k#1"]
    assert data["skippedByProduct"] == {"kindash": {"held": 4}}
    assert data["dispatchableByProduct"] == {"kindash": 1}
    assert data["skipped"] == {"held": 4}, "the fleet map is re-summed for the narrowed scope"

    # An unknown slug is an empty answer, not an error: the model should say
    # "nothing dispatchable", not retry.
    out = await _tool(tools, "get_priority_queue").handler({"product": "nope"})
    assert out.get("is_error") is not True
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["entries"] == [] and data["skippedByProduct"] == {}
    assert data["dispatchableByProduct"] == {} and data["skipped"] == {}


@pytest.mark.asyncio
async def test_resolve_triage_routes_by_decision_and_rejects_others():
    seen: list[str] = []

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ok": True})

    tools = build_tools(_api(handler))
    await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "accept"})
    await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "reject"})
    await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "dismiss"})
    bad = await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "maybe"})
    assert seen == ["/api/pm/triage/p1/accept", "/api/pm/triage/p1/reject",
                    "/api/pm/triage/p1/dismiss"]
    assert bad["is_error"] is True


@pytest.mark.asyncio
async def test_resolve_triage_rejects_a_bad_proposal_id_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "resolve_triage").handler(
        {"proposal_id": "../../sessions", "decision": "accept"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected proposal id must never reach the API"


@pytest.mark.asyncio
async def test_resolve_decision_routes_by_decision_and_sends_chosen_key_only_on_accept():
    seen: list[tuple[str, dict | None]] = []

    def handler(req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        seen.append((req.url.path, body))
        return httpx.Response(200, json={"ok": True})

    tools = build_tools(_api(handler))
    await _tool(tools, "resolve_decision").handler(
        {"proposal_id": "dp-1", "decision": "accept", "chosen_key": "b"})
    await _tool(tools, "resolve_decision").handler(
        {"proposal_id": "dp-1", "decision": "reject", "chosen_key": "b"})
    bad = await _tool(tools, "resolve_decision").handler(
        {"proposal_id": "dp-1", "decision": "maybe"})
    assert seen == [
        ("/api/pm/decisions/dp-1/accept", {"chosen_key": "b"}),
        ("/api/pm/decisions/dp-1/reject", None),
    ], "reject must never send chosen_key, even when the caller passed one"
    assert bad["is_error"] is True


@pytest.mark.asyncio
async def test_resolve_decision_requires_chosen_key_to_accept():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "resolve_decision").handler(
        {"proposal_id": "dp-1", "decision": "accept"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected accept must never reach the API"


@pytest.mark.asyncio
async def test_resolve_decision_rejects_a_bad_proposal_id_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "resolve_decision").handler(
        {"proposal_id": "../../sessions", "decision": "accept", "chosen_key": "b"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected proposal id must never reach the API"


def test_product_tool_classes():
    """The six reads must never be classed as mutations (they would then take a
    permission card), and the writers must never be classed READ (they would
    then skip one)."""
    for name in ("list_products", "get_product", "get_product_deps", "get_priority_queue",
                 "list_triage", "get_onboarding_status"):
        assert TOOL_SPECS[name].tool_class is ToolClass.READ
    for name in ("resolve_triage", "resolve_decision", "request_product_sync"):
        assert TOOL_SPECS[name].tool_class is ToolClass.MUTATION
        assert annotation_for(f"{MCP_PREFIX}{name}").tool_class is ToolClass.MUTATION


# --- Automation read tools (plan Task 6.3, Pack A) -----------------------------------

_JOB = "prd-1a2b3c4d"


# `payload` is the SHAPE the real route returns (a bare list for the two list routes, an
# object for the rest) — a one-size-fits-all `{}` would let a handler that mishandles its
# own payload still pass the path assertion.
@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,path,payload", [
    ("get_automation_actionable", {"id": "pr-doctor"},
     "/api/automations/pr-doctor/actionable", []),
    ("list_automation_sessions", {"id": "autopilot"},
     "/api/automations/autopilot/sessions", []),
    ("list_backlog_candidates", {}, "/api/automations/backlog-runner/candidates",
     {"candidates": []}),
    ("get_backlog_groups", {}, "/api/automations/backlog-runner/groups",
     {"generation": 3, "groups": []}),
    ("get_cicd_findings", {}, "/api/automations/cicd-doctor/findings", []),
    ("get_pr_doctor_consolidation", {}, "/api/automations/pr-doctor/consolidation",
     {"repos": {}, "clusters": []}),
    ("get_pr_doctor_job", {"job_id": _JOB}, f"/api/automations/pr-doctor/jobs/{_JOB}",
     {"job_id": _JOB, "status": "running", "phase": "cloning"}),
])
async def test_automation_read_tools_paths(name, args, path, payload):
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json=payload)

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert "is_error" not in out
    assert seen == [("GET", path)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_automation_actionable", "list_automation_sessions"])
async def test_automation_read_tools_reject_a_bad_id_locally(name):
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), name).handler({"id": "../../sessions"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected automation id must never reach the API"


@pytest.mark.asyncio
async def test_get_pr_doctor_job_rejects_a_bad_job_id_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "get_pr_doctor_job").handler(
        {"job_id": "../consolidation"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected job id must never reach the API"


@pytest.mark.asyncio
async def test_get_cicd_findings_forwards_filters_and_caps_the_list():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[
            {"id": f"f{i}", "repo": "acme/billing", "kind": "flaky", "title": f"t{i}",
             "status": "open"} for i in range(60)])

    out = await _tool(build_tools(_api(handler)), "get_cicd_findings").handler(
        {"status": "all", "repo": "acme/billing"})
    assert seen["params"] == {"status": "all", "repo": "acme/billing"}
    body = json.loads(out["content"][0]["text"])
    assert len(body["data"]) == 50
    assert body["truncated"] == 10


@pytest.mark.asyncio
async def test_get_cicd_findings_omits_unset_filters():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[])

    await _tool(build_tools(_api(handler)), "get_cicd_findings").handler({})
    assert seen["params"] == {}, "unset filters must let the server defaults apply"


@pytest.mark.asyncio
async def test_list_backlog_candidates_caps_the_list_and_reports_the_drop():
    def handler(req: httpx.Request):
        return httpx.Response(200, json={
            "candidates": [{"ref": f"acme/billing#{i}"} for i in range(60)]})

    out = await _tool(build_tools(_api(handler)), "list_backlog_candidates").handler({})
    body = json.loads(out["content"][0]["text"])
    assert len(body["data"]["candidates"]) == 50
    assert body["truncated"] == 10


@pytest.mark.asyncio
async def test_preview_schedule_posts_the_endpoints_body_field():
    seen = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"nextRuns": [], "presets": {}})

    schedule = {"cron": "0 8 * * *"}
    out = await _tool(build_tools(_api(handler)), "preview_schedule").handler(
        {"schedule": schedule})
    assert "is_error" not in out
    assert seen == {"method": "POST", "path": "/api/automations/schedule/preview",
                    "body": {"schedule": schedule}}


def test_automation_read_tool_classes_and_the_read_post_escape_hatch():
    """All eight are READ — a MUTATION class would put a permission card in front of a
    plain look. `preview_schedule` is the one that reaches a POST, so it must be named
    in READ_POSTS or the conformance gate (rightly) rejects it."""
    from server.assistant.policy import READ_POSTS

    for name in ("get_automation_actionable", "list_automation_sessions",
                 "list_backlog_candidates", "get_backlog_groups", "get_cicd_findings",
                 "get_pr_doctor_consolidation", "get_pr_doctor_job", "preview_schedule"):
        assert TOOL_SPECS[name].tool_class is ToolClass.READ
        assert annotation_for(f"{MCP_PREFIX}{name}").tool_class is ToolClass.READ
    assert "preview_schedule" in READ_POSTS


# --- Session-start conveniences (plan Task 7.3, Pack C) ------------------------------


@pytest.mark.asyncio
async def test_list_presets_reads_the_presets_route():
    def h(req: httpx.Request):
        assert req.method == "GET"
        assert req.url.path == "/api/presets"
        return httpx.Response(200, json=[{"id": "p1", "name": "review"}])

    out = await _tool(build_tools(_api(h)), "list_presets").handler({})
    assert "is_error" not in out
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == [{"id": "p1", "name": "review"}]


@pytest.mark.asyncio
async def test_list_templates_passes_tag_only_when_given():
    seen = {}

    def h(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[{"id": "t1", "name": "standup", "tags": ["bugfix"]}])

    out = await _tool(build_tools(_api(h)), "list_templates").handler({"tag": "bugfix"})
    assert "is_error" not in out
    assert seen["params"] == {"tag": "bugfix"}

    seen.clear()
    await _tool(build_tools(_api(h)), "list_templates").handler({})
    assert seen["params"] == {}, "an unset tag must let the server return everything"


@pytest.mark.asyncio
async def test_render_template_is_a_read_shaped_post():
    from server.assistant.policy import READ_POSTS

    def h(req: httpx.Request):
        assert req.method == "POST"
        assert req.url.path == "/api/templates/t1/render"
        assert json.loads(req.content) == {"variables": {"name": "x"}}
        return httpx.Response(200, json={"rendered": "hi x"})

    out = await _tool(build_tools(_api(h)), "render_template").handler(
        {"id": "t1", "vars": {"name": "x"}})
    assert "is_error" not in out
    assert json.loads(out["content"][0]["text"])["data"] == {"rendered": "hi x"}
    assert TOOL_SPECS["render_template"].tool_class is ToolClass.READ
    assert "render_template" in READ_POSTS


@pytest.mark.asyncio
async def test_render_template_defaults_vars_to_an_empty_object():
    def h(req: httpx.Request):
        assert json.loads(req.content) == {"variables": {}}
        return httpx.Response(200, json={"rendered": ""})

    out = await _tool(build_tools(_api(h)), "render_template").handler({"id": "t1"})
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_render_template_rejects_a_bad_id_locally():
    calls: list[str] = []

    def h(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={"rendered": ""})

    out = await _tool(build_tools(_api(h)), "render_template").handler(
        {"id": "../templates", "vars": {}})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected template id must never reach the API"

    out = await _tool(build_tools(_api(h)), "render_template").handler(
        {"id": "not a valid id", "vars": {}})
    assert out["is_error"] is True
    assert calls == []


@pytest.mark.asyncio
async def test_list_permission_profiles_reads_the_permission_profiles_route():
    def h(req: httpx.Request):
        assert req.method == "GET"
        assert req.url.path == "/api/permission-profiles"
        return httpx.Response(200, json=[{"id": "pp1", "name": "read-only"}])

    out = await _tool(build_tools(_api(h)), "list_permission_profiles").handler({})
    assert "is_error" not in out
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == [{"id": "pp1", "name": "read-only"}]


# --- Automation fan-out tools (plan Task 6.4, Pack A) --------------------------------
# The half of Pack A that STARTS unattended work: each of these spawns a coding session
# that acts on its own. Every test here is about that fact — the class, the card, and
# the local arg checks that stop a malformed fan-out before it reaches the API.

_FAN_OUT_TOOLS = ("trigger_automation_action", "dispatch_backlog", "consolidate_prs")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,path,body", [
    ("trigger_automation_action",
     {"id": "pr-doctor", "item_id": "acme/checkout#10", "kind": "failing_ci"},
     "/api/automations/pr-doctor/trigger",
     {"item_id": "acme/checkout#10", "kind": "failing_ci"}),
    ("dispatch_backlog", {"refs": ["acme/billing#18", "acme/billing#20"]},
     "/api/automations/backlog-runner/dispatch",
     {"refs": ["acme/billing#18", "acme/billing#20"]}),
    ("consolidate_prs", {"repo": "acme/billing", "numbers": [42, 43]},
     "/api/automations/pr-doctor/consolidate",
     {"repo": "acme/billing", "numbers": [42, 43]}),
])
async def test_fan_out_tools_post_the_endpoints_body(name, args, path, body):
    seen: dict[str, object] = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "spawned", "session_id": "s1"})

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert "is_error" not in out
    assert seen == {"method": "POST", "path": path, "body": body}


def test_fan_out_tools_are_destructive():
    """DESTRUCTIVE is what gets these a permission card in EVERY mode (including
    full-auto) with no "Always allow" — see server/sessions/permissions.py, which reads
    the class off `annotation_for`, so both lookups have to agree."""
    for name in _FAN_OUT_TOOLS:
        assert TOOL_SPECS[name].tool_class is ToolClass.DESTRUCTIVE
        assert annotation_for(f"{MCP_PREFIX}{name}").tool_class is ToolClass.DESTRUCTIVE


def test_fan_out_tool_descriptions_say_the_work_is_unattended():
    """The model's only warning that a call is irreversible is the description it reads
    before choosing the tool, so the wording is load-bearing, not decoration."""
    tools = build_tools(_api(lambda r: httpx.Response(200, json={})))
    for name in _FAN_OUT_TOOLS:
        text = _tool(tools, name).description.lower()
        assert "on its own" in text and "no undo" in text, f"{name}: {text}"


@pytest.mark.asyncio
async def test_trigger_automation_action_rejects_a_bad_id_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "trigger_automation_action").handler(
        {"id": "../../sessions", "item_id": "acme/checkout#10", "kind": "failing_ci"})
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected automation id must never reach the API"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"id": "pr-doctor", "item_id": "", "kind": "failing_ci"},
    {"id": "pr-doctor", "item_id": "acme/checkout#10", "kind": ""},
])
async def test_trigger_automation_action_requires_item_and_kind(args):
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "trigger_automation_action").handler(args)
    assert out["is_error"] is True
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refs", [[], ["acme/billing#18", ""], "acme/billing#18"])
async def test_dispatch_backlog_rejects_a_bad_ref_list_locally(refs):
    """An empty list is a 400 from the endpoint and a bare string would be a 422; both
    are opaque to the model, so they are rejected here as a corrective message instead."""
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "dispatch_backlog").handler({"refs": refs})
    assert out["is_error"] is True
    assert calls == []


@pytest.mark.asyncio
async def test_consolidate_prs_coerces_numbers_and_rejects_non_numeric():
    seen: dict[str, object] = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "spawned"})

    tools = build_tools(_api(handler))
    # The endpoint's body model is `numbers: list[int]`; a JSON-schema "integer" array
    # still reaches the handler as whatever the model emitted, so coerce rather than 422.
    await _tool(tools, "consolidate_prs").handler({"repo": "acme/billing", "numbers": ["42", 43]})
    assert seen["body"] == {"repo": "acme/billing", "numbers": [42, 43]}

    # Rejected rather than silently read as 42 / 1 — a PR number that arrived as a
    # fraction or a boolean means the model got the argument wrong, and consolidating
    # the WRONG PR is not a mistake a permission card can catch.
    for bad in (["forty-two"], [42.7], [True]):
        out = await _tool(tools, "consolidate_prs").handler(
            {"repo": "acme/billing", "numbers": bad})
        assert out["is_error"] is True, bad


@pytest.mark.asyncio
async def test_consolidate_prs_rejects_an_empty_number_list_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), "consolidate_prs").handler(
        {"repo": "acme/billing", "numbers": []})
    assert out["is_error"] is True


# --- Session Feedback inbox tools (#2936) --------------------------------------------
# Thin adapters over the clustered-inbox routes (server/api/automations.py). The two
# reads change nothing; the four writes file/comment publicly or move reports out of
# the inbox, so each raises a card like trigger_automation_action.

_FB = "/api/automations/session-feedback/clusters"
_INBOX_WRITE_TOOLS = ("file_feedback_cluster", "dismiss_feedback_cluster",
                      "attach_feedback_to_issue", "attach_feedback_to_session")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,method,path,body", [
    ("get_feedback_clusters", {}, "GET", _FB, None),
    ("draft_feedback_cluster_issue", {"ids": ["r1", "r2"]}, "POST", f"{_FB}/draft",
     {"ids": ["r1", "r2"]}),
    ("file_feedback_cluster", {"ids": ["r1"], "title": "T", "body": "B"}, "POST",
     f"{_FB}/file", {"ids": ["r1"], "title": "T", "body": "B"}),
    ("dismiss_feedback_cluster", {"ids": ["r1", "r2"]}, "POST", f"{_FB}/dismiss",
     {"ids": ["r1", "r2"]}),
    ("dismiss_feedback_cluster", {"ids": ["r1"], "fixed_by": "2950"}, "POST",
     f"{_FB}/dismiss", {"ids": ["r1"], "fixedBy": 2950}),
    ("attach_feedback_to_issue", {"ids": ["r1"], "issue": "77"}, "POST",
     f"{_FB}/attach", {"ids": ["r1"], "issue": 77}),
    ("attach_feedback_to_session", {"ids": ["r1"], "session_id": "s-9"}, "POST",
     f"{_FB}/attach", {"ids": ["r1"], "sessionId": "s-9"}),
])
async def test_feedback_inbox_tools_hit_their_endpoint(name, args, method, path, body):
    seen: list[tuple[str, str, object]] = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"status": "ok", "level": "ok", "message": "done"})

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert "is_error" not in out
    assert seen == [(method, path, body)]


@pytest.mark.asyncio
async def test_get_feedback_clusters_caps_the_cluster_list():
    rows = [{"id": f"c{i}", "title": "t", "count": 1, "members": []} for i in range(60)]

    def handler(req: httpx.Request):
        return httpx.Response(200, json={"clusters": rows, "llm": "applied", "total": 60})

    out = await _tool(build_tools(_api(handler)), "get_feedback_clusters").handler({})
    body = json.loads(out["content"][0]["text"])
    assert len(body["data"]["clusters"]) == 50 and body["data"]["total"] == 60
    assert body["truncated"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("draft_feedback_cluster_issue", {"ids": []}),
    ("draft_feedback_cluster_issue", {"ids": "r1"}),
    ("file_feedback_cluster", {"ids": ["r1"], "title": "", "body": "B"}),
    ("file_feedback_cluster", {"ids": ["r1"], "title": "T", "body": "  "}),
    ("file_feedback_cluster", {"ids": [], "title": "T", "body": "B"}),
    ("dismiss_feedback_cluster", {"ids": ["r1", ""]}),
    ("dismiss_feedback_cluster", {"ids": ["r1"], "fixed_by": 12.5}),
    ("attach_feedback_to_issue", {"ids": ["r1"]}),
    ("attach_feedback_to_issue", {"ids": ["r1"], "issue": "seventy"}),
    ("attach_feedback_to_session", {"ids": ["r1"], "session_id": ""}),
])
async def test_feedback_inbox_tools_reject_bad_args_locally(name, args):
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert out["is_error"] is True
    assert calls == [], "a locally-rejected argument must never reach the API"


def test_feedback_inbox_classes():
    """The reads change nothing (draft is a POST that files nothing — READ_POSTS); the
    writes file or comment publicly, or empty the inbox, so each gets a card in every
    mode exactly like trigger_automation_action."""
    from server.assistant.policy import READ_POSTS
    for name in ("get_feedback_clusters", "draft_feedback_cluster_issue"):
        assert TOOL_SPECS[name].tool_class is ToolClass.READ
    assert "draft_feedback_cluster_issue" in READ_POSTS
    for name in _INBOX_WRITE_TOOLS:
        assert TOOL_SPECS[name].tool_class is TOOL_SPECS["trigger_automation_action"].tool_class
        assert annotation_for(f"{MCP_PREFIX}{name}").tool_class is ToolClass.DESTRUCTIVE


def test_feedback_inbox_write_descriptions_name_the_effect_and_the_card():
    tools = build_tools(_api(lambda r: httpx.Response(200, json={})))
    effects = {"file_feedback_cluster": "public", "dismiss_feedback_cluster": "inbox",
               "attach_feedback_to_issue": "public", "attach_feedback_to_session": "prompt"}
    for name, effect in effects.items():
        text = _tool(tools, name).description.lower()
        assert "permission card" in text and effect in text, f"{name}: {text}"


# ---------------------------------------------------------------------------
# Batch tools (spec v2 B4): archive_sessions, unarchive_sessions, resume_sessions,
# rotate_sessions, delete_sessions.
# ---------------------------------------------------------------------------

def test_batch_items_returns_titles():
    assert batch_items(MCP_PREFIX + "archive_sessions",
                       {"ids": ["a"], "titles": ["Fix auth"]}) == ["Fix auth"]
    assert batch_items(MCP_PREFIX + "archive_session", {"id": "a"}) is None
    assert batch_items(MCP_PREFIX + "list_sessions", {}) is None
    # Malformed titles yield an empty list rather than raising — the tool handler's
    # own validation (_batch_args) is what turns that into a corrective error.
    assert batch_items(MCP_PREFIX + "delete_sessions", {"ids": ["a"]}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,path", [
    ("archive_sessions", "/api/sessions/batch/archive"),
    ("unarchive_sessions", "/api/sessions/batch/unarchive"),
    ("resume_sessions", "/api/sessions/batch/resume"),
    ("rotate_sessions", "/api/sessions/batch/rotate"),
    ("delete_sessions", "/api/sessions/batch/permanent-delete"),
])
async def test_batch_tools_post_ids_to_their_endpoint(name, path):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"succeeded": [], "failed": []})

    tools = build_tools(_api(handler))
    ids = ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]
    out = await _tool(tools, name).handler({"ids": ids, "titles": ["A", "B"]})
    assert out.get("is_error") is not True
    assert seen == {"path": path, "body": {"ids": ids}}


@pytest.mark.asyncio
async def test_archive_sessions_batch_validates_titles_and_size():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={"succeeded": [], "failed": []})

    tools = build_tools(_api(handler))
    ids = ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]

    # titles must name every id, in the same order.
    bad = await _tool(tools, "archive_sessions").handler({"ids": ids, "titles": ["A"]})
    assert bad["is_error"] is True

    # at most 20 sessions per batch.
    too_many = await _tool(tools, "archive_sessions").handler(
        {"ids": ids * 11, "titles": ["x"] * 22})
    assert too_many["is_error"] is True

    assert calls == [], "a rejected batch must never reach the API"
    assert calls == []


# --------------------------------------------------------------------------- #
# Pack G writes (Task 7.6): comment_on_pr / create_issue                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_comment_on_pr_posts_to_the_pr_comments_route():
    seen: dict = {}

    def handler(req: httpx.Request):
        seen["call"] = (req.method, req.url.path, json.loads(req.content))
        return httpx.Response(201, json={"url": "https://c/1"})

    out = await _tool(build_tools(_api(handler)), "comment_on_pr").handler(
        {"repo": "acme/billing", "number": 42, "body": "  needs a rollback plan  "})
    assert out.get("is_error") is not True
    assert seen["call"] == ("POST", "/api/prs/acme/billing/42/comments",
                            {"body": "needs a rollback plan"})


@pytest.mark.asyncio
async def test_create_issue_posts_repo_title_body_and_optional_labels():
    seen: list[tuple] = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(201, json={"number": 21, "url": "https://i/21"})

    tools = build_tools(_api(handler))
    await _tool(tools, "create_issue").handler(
        {"repo": "acme/billing", "title": "Flaky test", "body": "fails 1 in 5",
         "labels": ["bug"]})
    # labels is optional and must be omitted entirely when unset, so the endpoint's
    # "no labels" default applies rather than an empty list meaning something else.
    await _tool(tools, "create_issue").handler(
        {"repo": "acme/billing", "title": "Flaky test", "body": "fails 1 in 5"})

    assert seen[0] == ("POST", "/api/github/issues",
                       {"repo": "acme/billing", "title": "Flaky test",
                        "body": "fails 1 in 5", "labels": ["bug"]})
    assert "labels" not in seen[1][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("comment_on_pr", {"repo": "acme", "number": 1, "body": "b"}),
    ("comment_on_pr", {"repo": "acme/billing", "number": 1, "body": "   "}),
    ("comment_on_pr", {"repo": "acme/billing", "number": 1, "body": "x" * 10001}),
    ("create_issue", {"repo": "acme/billing/extra", "title": "t", "body": "b"}),
    ("create_issue", {"repo": "acme/billing", "title": "", "body": "b"}),
    ("create_issue", {"repo": "acme/billing", "title": "t", "body": "x" * 10001}),
])
async def test_github_write_tools_reject_bad_args_locally(name, args):
    """A public write must be refused BEFORE it reaches the API — a 422 comes back to the
    model as an opaque API error, where a local ToolArgError names the field to fix."""
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(201, json={})

    out = await _tool(build_tools(_api(handler)), name).handler(args)
    assert out["is_error"] is True
    assert calls == []


def test_github_write_tools_are_mutation_class():
    """Not READ (they leave ViberPit) and not DESTRUCTIVE (nothing is lost); MUTATION is
    what puts a confirmation card in front of a public post."""
    for name in ("comment_on_pr", "create_issue"):
        assert TOOL_SPECS[name].tool_class is ToolClass.MUTATION


def test_pipeline_and_session_descriptions_match_available_data():
    tools = build_tools(_api(lambda _: httpx.Response(200, json={})))
    stages = _tool(tools, "list_stage_models").description
    assert "structural" in stages
    assert "typical durations" not in stages
    assert "regressedChecks" not in stages
    assert "get_pipeline_health" in stages
    detail = _tool(tools, "get_session").description
    assert "Does not include pending permission or question ids" in detail
    question = _tool(tools, "answer_question").description
    assert "get_session does not return them" in question
    assert "ask the user" in question


def test_cap_result_halves_the_longest_list_until_it_fits():
    from server.assistant.tools import cap_result
    data = {"project": {"title": "x"}, "edges": [{"src": f"o/r#{i}", "dst": "o/r#0",
                                                  "source": "native"} for i in range(4000)],
            "drift": {"proseOnly": [{"src": "a", "dst": "b"}] * 5}}
    out, capped = cap_result(data, max_bytes=20_000)
    assert capped["path"] == "edges"
    assert capped["kept"] == len(out["edges"]) and capped["dropped"] == 4000 - capped["kept"]
    assert len(json.dumps(out, separators=(",", ":"))) <= 20_000
    assert out["drift"] == data["drift"], "only the longest list is cut"


def test_cap_result_cuts_a_top_level_list_and_a_list_one_level_down():
    from server.assistant.tools import cap_result
    rows = [{"n": i, "pad": "x" * 100} for i in range(2000)]
    out, capped = cap_result(rows, max_bytes=10_000)
    assert capped["path"] == "" and len(out) < 2000
    out, capped = cap_result({"data": {"items": rows}}, max_bytes=10_000)
    assert capped["path"] == "data.items" and len(out["data"]["items"]) < 2000


def test_cap_result_leaves_a_small_or_uncuttable_payload_alone():
    from server.assistant.tools import cap_result
    small = {"a": [1, 2, 3]}
    assert cap_result(small, max_bytes=1_000) == (small, None)
    # Over the cap but nothing to shrink: returned as-is, never an infinite loop.
    fat = {f"k{i}": "y" * 1_900 for i in range(30)}
    out, capped = cap_result(fat, max_bytes=1_000)
    assert out == fat and capped is None


@pytest.mark.asyncio
async def test_guarded_envelope_reports_the_cap_under_its_own_key():
    from server.assistant import tools as tools_mod
    big = [{"repo": "o/r", "number": i, "title": "t" * 200, "state": "open",
            "stage": "ci", "checks": "pending"} for i in range(3000)]

    def handler(req: httpx.Request):
        return httpx.Response(200, json=big)

    with patch.object(tools_mod, "_MAX_RESULT_BYTES", 30_000):
        out = await _tool(build_tools(_api(handler)), "list_prs").handler({"limit": 3000})
    body = json.loads(out["content"][0]["text"])
    assert body["capped"]["path"] == "" and body["capped"]["dropped"] > 0
    assert len(out["content"][0]["text"]) <= 30_000 + 512  # envelope overhead only


_PR_ROW = {"repo": "o/r", "number": 1, "title": "t", "state": "open", "merged": False,
           "is_draft": False, "stage": "ci", "checks": "pending", "mergeable": True,
           "merge_state_status": "CLEAN", "auto_merge_enabled": True, "url": "u",
           "head_ref": "b", "updated_at": "2026-01-01T00:00:00Z", "in_merge_queue": False,
           "session": {"id": "s1", "name": "n"},
           "checks_detail": [{"name": "x"}] * 40, "check_groups": [{"checks": [{}] * 20}] * 5,
           "node_states": [{}] * 10, "deployed_envs": [], "eta_seconds": 1}


@pytest.mark.asyncio
async def test_list_prs_projects_rows_filters_state_and_limits():
    rows = [dict(_PR_ROW, number=1),
            dict(_PR_ROW, number=2, state="draft", is_draft=True),
            dict(_PR_ROW, number=3, state="merged", merged=True, stage="merged"),
            dict(_PR_ROW, number=4, state="closed")]

    def handler(req: httpx.Request):
        return httpx.Response(200, json=rows)

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_prs").handler({}))["content"][0]["text"])
    numbers = [p["number"] for p in out["data"]]
    assert numbers == [1, 2], "default state=open keeps drafts, drops merged/closed"
    row = out["data"][0]
    assert set(row) == {"repo", "number", "title", "state", "is_draft", "stage", "checks",
                        "mergeable", "merge_state_status", "auto_merge_enabled", "url",
                        "head_ref", "updated_at", "in_merge_queue", "session"}
    out = json.loads((await _tool(tools, "list_prs").handler(
        {"state": "all", "limit": 3}))["content"][0]["text"])
    assert [p["number"] for p in out["data"]] == [1, 2, 3] and out["truncated"] == 1
    out = json.loads((await _tool(tools, "list_prs").handler(
        {"state": "merged"}))["content"][0]["text"])
    assert [p["number"] for p in out["data"]] == [3]
    out = json.loads((await _tool(tools, "list_prs").handler(
        {"verbose": True}))["content"][0]["text"])
    assert "checks_detail" in out["data"][0] and len(out["data"]) <= 5


@pytest.mark.asyncio
async def test_list_automations_projects_by_default_and_keeps_products():
    row = {"id": "backlog-runner", "name": "Backlog Runner", "description": "d" * 500,
           "enabled": True, "mode": "auto", "modes": ["off", "auto"],
           "status": {"text": "1 work in flight", "level": "active",
                      "counts": {"eligible": 3, "held": 4},
                      "products": [{"slug": "kindash", "eligible": 0, "held": 4}],
                      "waitingReason": "human"},
           "actionableCount": 2, "parkedCount": 0, "paused": False, "oversight": {"x": 1},
           "configSchema": [{"key": "k"}] * 20, "enginePolicy": "claude_only",
           "operatorTriggered": False, "workClass": "development",
           "config": {"max_concurrent": 2}, "schedule": None,
           "lastActiveAt": "2026-01-01T00:00:00Z", "activeSessions": 1}

    def handler(req: httpx.Request):
        return httpx.Response(200, json=[row])

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_automations").handler({}))["content"][0]["text"])
    a = out["data"][0]
    assert set(a) == {"id", "name", "enabled", "mode", "status", "actionableCount",
                      "parkedCount", "lastActiveAt", "activeSessions", "workClass"}
    assert a["status"]["products"] == row["status"]["products"]
    assert a["status"]["counts"] == {"eligible": 3, "held": 4}
    out = json.loads((await _tool(tools, "list_automations").handler(
        {"verbose": True}))["content"][0]["text"])
    assert "configSchema" in out["data"][0] and "config" in out["data"][0]


_DECISION_ROW = {"id": "dp-1", "workItemId": "wi-1", "repo": "o/k", "number": 4,
                 "ref": "o/k#4", "projectSlug": "kindash", "title": "Retry policy",
                 "url": "https://x/4", "options": [{"key": "a", "label": "A", "detail": None},
                                                   {"key": "b", "label": "B", "detail": "d"}],
                 "recommendedKey": "b", "rationale": "why", "chosenKey": None,
                 "status": "pending", "proposedBy": "session:s1", "resolvedBy": None,
                 "createdAt": "2026-01-01T00:00:00+00:00", "resolvedAt": None,
                 "guidance": {"accept": {"big": "x" * 500}, "reject": {}}}


@pytest.mark.asyncio
async def test_list_decisions_filters_by_product_projects_rows_and_limits():
    seen: dict = {}

    def handler(req: httpx.Request):
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return httpx.Response(200, json=[dict(_DECISION_ROW, id=f"dp-{i}") for i in range(30)])

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_decisions").handler({}))["content"][0]["text"])
    assert seen == {"path": "/api/pm/decisions", "params": {}}, "impact is off unless asked"
    assert len(out["data"]) == 20 and out["truncated"] == 10
    assert set(out["data"][0]) == {"id", "ref", "product", "title", "url", "options",
                                   "recommendedKey", "rationale", "proposedBy", "createdAt"}
    assert out["data"][0]["product"] == "kindash"

    await _tool(tools, "list_decisions").handler({"product": "kindash", "impact": True,
                                                  "limit": 500})
    assert seen["params"] == {"projectSlug": "kindash", "impact": "true"}

    def with_impact(req: httpx.Request):
        return httpx.Response(200, json=[dict(_DECISION_ROW, downstream=7,
                                              downstreamDispatchable=4)])
    out = json.loads((await _tool(build_tools(_api(with_impact)), "list_decisions").handler(
        {"impact": True}))["content"][0]["text"])
    assert out["data"][0]["downstream"] == 7 and out["data"][0]["downstreamDispatchable"] == 4


_HELD_ROW = {"ref": "o/k#4", "repo": "o/k", "number": 4, "product": "kindash",
             "title": "Retry policy", "url": "https://x/4", "holdKind": "decision",
             "holdReason": "decision pending: why", "holdBy": "session:s1",
             "holdSetAt": "2026-01-01T00:00:00+00:00", "ageHours": 31.2,
             "proposalId": "dp-1", "options": [{"key": "a", "label": "A", "detail": None},
                                               {"key": "b", "label": "B", "detail": "d"}],
             "recommendedKey": "b", "downstream": 7, "downstreamDispatchable": 4,
             "downstreamRefs": ["o/k#5", "o/k#6"], "dispatchableOnRelease": True,
             "releasedBy": "Accept an option in the Inbox"}


@pytest.mark.asyncio
async def test_list_holds_forwards_filters_and_projects_rows():
    seen: dict = {}

    def handler(req: httpx.Request):
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return httpx.Response(200, json={
            "products": [{"slug": "kindash", "dispatchable": 0, "skipped": {"held": 3},
                          "heldCount": 3, "starving": True, "oldestHoldHours": 212.4}],
            "held": [_HELD_ROW], "truncated": 2, "generatedAt": "2026-01-01T00:00:00+00:00"})

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_holds").handler(
        {"product": "kindash", "kind": "decision", "limit": 99}))["content"][0]["text"])
    assert seen == {"path": "/api/pm/holds",
                    "params": {"product": "kindash", "kind": "decision", "limit": "50"}}
    assert out["data"]["products"][0]["starving"] is True
    row = out["data"]["held"][0]
    assert set(row) == {"ref", "product", "title", "url", "holdKind", "holdReason", "holdBy",
                        "ageHours", "proposalId", "recommendedKey", "optionKeys", "downstream",
                        "downstreamDispatchable", "releasedBy"}
    assert row["optionKeys"] == ["a", "b"] and row["proposalId"] == "dp-1"
    assert out["truncated"] == 2, "the server's own drop count rides the envelope"

    # Nothing held: the product summary still answers "is it starving?".
    def empty(req: httpx.Request):
        return httpx.Response(200, json={"products": [{"slug": "kindash", "dispatchable": 5,
                                                       "skipped": {}, "heldCount": 0,
                                                       "starving": False,
                                                       "oldestHoldHours": None}],
                                         "held": [], "truncated": 0, "generatedAt": "x"})
    out = json.loads((await _tool(build_tools(_api(empty)), "list_holds").handler({}))
                     ["content"][0]["text"])
    assert out["data"]["held"] == [] and out["data"]["products"][0]["heldCount"] == 0
    assert "truncated" not in out


@pytest.mark.asyncio
async def test_list_holds_rejects_an_unknown_kind_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})
    out = await _tool(build_tools(_api(handler)), "list_holds").handler({"kind": "evidence"})
    assert out["is_error"] is True and calls == []


@pytest.mark.asyncio
async def test_resolve_decision_error_points_at_list_decisions():
    out = await _tool(build_tools(_api(lambda r: httpx.Response(200, json={}))),
                      "resolve_decision").handler({"proposal_id": "not valid!", "decision": "reject"})
    assert out["is_error"] is True and "list_decisions" in out["content"][0]["text"]
