from __future__ import annotations

import json

import httpx
import jsonschema
import pytest
from mcp.types import ListToolsRequest

from server.assistant.client import (AssistantApi, AssistantApiError,  # noqa: F401
                                     TOOL_CALL_CAPACITY)
from server.assistant.server import build_server
from server.assistant.tools import (OPERATOR_TOOLS, ToolClass, TOOL_SPECS, annotation_for,
                                    build_tools, wrap_untrusted)


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
        seen["via"] = req.headers["x-ccswitch-via"]
        seen["asst"] = req.headers["x-ccswitch-assistant-session"]
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
            {"type": "user", "text": "<ccswitch-context route=\"/x\"/> hi"},
            {"type": "textDelta", "text": "A" * 5000},
            {"type": "toolCall", "name": "Bash", "input": {"command": "ls"}},
        ], "earliestTurn": 0, "totalTurns": 1})

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_session_transcript").handler({"id": "abc", "last_n": 5})
    text = out["content"][0]["text"]
    assert "<ccswitch-context" not in text
    assert "&lt;ccswitch-context" in text
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
    assert annotation_for("mcp__ccswitch__list_sessions").tool_class is ToolClass.READ
    assert annotation_for("mcp__other__list_sessions") is None
    for t in build_tools(_api(lambda r: httpx.Response(200, json={}))):
        assert t.name in TOOL_SPECS, f"tool {t.name} has no ToolSpec"
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is (TOOL_SPECS[t.name].tool_class is ToolClass.READ)


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
                       "get_cicd_findings", "get_pr_doctor_consolidation")


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
      "permissionMode": "interactive", "account": "acct"}),
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
     "PUT", "/api/notifications/quiet-hours", {"start": "22:00", "end": "07:00"}),
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
        return httpx.Response(201, json={"id": "sess-9", "name": "x", "cwd": "/secret"})

    out = await _tool(build_tools(_api(handler)), "create_session").handler({"prompt": "go"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == {"id": "sess-9", "url": "/sessions/sess-9"}


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
    monkeypatch.setenv("CCSWITCH_DB_URL", url)
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
    monkeypatch.setenv("CCSWITCH_DB_URL", url)
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
            "skipped": {"deps": 4, "status": 9},
            "generatedAt": "2026-09-05T00:00:00+00:00",
        })

    out = await _tool(build_tools(_api(handler)), "get_priority_queue").handler(
        {"min_status": "backlog"})
    assert seen == {"method": "GET", "path": "/api/pm/queue",
                    "params": {"min_status": "backlog"}}
    body = json.loads(out["content"][0]["text"])
    # Capped, but the envelope still reports the drop, so the model never states a
    # truncated queue as if it were the whole one.
    assert len(body["data"]["entries"]) == 50
    assert body["truncated"] == 10
    # `skipped` (reason -> count) is what answers "what is blocked by a dependency?",
    # so it must survive the trim untouched.
    assert body["data"]["skipped"] == {"deps": 4, "status": 9}


@pytest.mark.asyncio
async def test_get_priority_queue_omits_min_status_when_unset():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"entries": [], "skipped": {}})

    await _tool(build_tools(_api(handler)), "get_priority_queue").handler({})
    assert seen["params"] == {}, "an unset min_status must let the server default apply"


@pytest.mark.asyncio
async def test_resolve_triage_routes_by_decision_and_rejects_others():
    seen: list[str] = []

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ok": True})

    tools = build_tools(_api(handler))
    await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "accept"})
    await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "reject"})
    bad = await _tool(tools, "resolve_triage").handler({"proposal_id": "p1", "decision": "maybe"})
    assert seen == ["/api/pm/triage/p1/accept", "/api/pm/triage/p1/reject"]
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


def test_product_tool_classes():
    """The six reads must never be classed as mutations (they would then take a
    permission card), and the two writers must never be classed READ (they would
    then skip one)."""
    for name in ("list_products", "get_product", "get_product_deps", "get_priority_queue",
                 "list_triage", "get_onboarding_status"):
        assert TOOL_SPECS[name].tool_class is ToolClass.READ
    for name in ("resolve_triage", "request_product_sync"):
        assert TOOL_SPECS[name].tool_class is ToolClass.MUTATION
        assert annotation_for(f"mcp__ccswitch__{name}").tool_class is ToolClass.MUTATION


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
        assert annotation_for(f"mcp__ccswitch__{name}").tool_class is ToolClass.READ
    assert "preview_schedule" in READ_POSTS


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
        assert annotation_for(f"mcp__ccswitch__{name}").tool_class is ToolClass.DESTRUCTIVE


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
    assert calls == []
