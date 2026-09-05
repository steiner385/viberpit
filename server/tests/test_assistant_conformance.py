"""Every ccswitch tool hits an allowlisted route; every tool and every starter chip has an eval
case (spec v2 §4.0)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from server.assistant.client import AssistantApi
from server.assistant.policy import READ_POSTS, route_allowed
from server.assistant.tools import TOOL_SPECS, ToolClass, build_tools

CASES = Path("docs/superpowers/evals/assistant/cases.jsonl")
UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

# Minimal valid args per tool so every handler reaches its HTTP call. A tool missing here
# fails loudly (KeyError) — add its args when you add the tool.
ARGS: dict[str, dict] = {
    "list_sessions": {}, "get_session": {"id": UUID}, "get_session_transcript": {"id": UUID},
    "get_tool_result": {"id": UUID, "tool_id": "c1"}, "export_session": {"id": UUID},
    "search_sessions": {"query": "x"}, "list_archived_sessions": {}, "list_accounts": {},
    "list_hosts": {}, "list_prs": {}, "get_pipeline_health": {"repo": "o/n"},
    "list_repos": {}, "list_branches": {"repo": "o/n"}, "list_stage_models": {},
    "list_automations": {}, "get_automation_events": {"id": "autopilot"}, "get_deploy_status": {},
    "list_notification_channels": {}, "list_notification_rules": {}, "get_quiet_hours": {},
    "test_notification": {"event_class": "session.done"},
    "report_gap": {"capability": "x"},
    "create_session": {"prompt": "p"}, "send_prompt": {"id": UUID, "text": "t"},
    "interrupt_session": {"id": UUID},
    "answer_question": {"id": UUID, "question_id": "q1", "selected": ["a"]},
    "resolve_permission": {"id": UUID, "perm_id": "p1", "decision": "allow"},
    "set_model": {"id": UUID, "model": "opus"}, "set_permission_mode": {"id": UUID, "mode": "auto"},
    "resolve_model_approval": {"id": UUID, "decision": "approve"},
    "archive_session": {"id": UUID}, "unarchive_session": {"id": UUID},
    "set_autopilot_paused": {"id": UUID, "paused": True}, "fix_pr": {"id": UUID, "number": 1},
    "set_automation_enabled": {"id": "autopilot", "enabled": False},
    "create_notification_rule": {"event_class": "session.done", "action": "push"},
    "delete_notification_rule": {"id": "rule-1"}, "set_quiet_hours": {"start": "22:00"},
    "delete_session": {"id": UUID}, "cancel_queued_session": {"id": UUID},
    "ready_merge_pr": {"repo": "o/n", "number": 1},
}


def _recording_api(seen: list[tuple[str, str]]) -> AssistantApi:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"events": [], "id": UUID})
    return AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x", session_id="s",
                        transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(TOOL_SPECS))
async def test_every_tool_hits_an_allowed_route(name):
    seen: list[tuple[str, str]] = []
    tools = build_tools(_recording_api(seen))
    tool = next(t for t in tools if t.name == name)
    await tool.handler(ARGS[name])
    if name == "report_gap":
        # The sole exemption: the gap it records IS that no endpoint exists, so it
        # must reach the API zero times. Asserted rather than skipped.
        assert seen == [], "report_gap must not call the API"
        return
    assert len(seen) == 1, f"{name} made {len(seen)} calls; a tool is ONE endpoint"
    method, path = seen[0]
    assert route_allowed(method, path), f"{name} hit {method} {path}, not allowlisted"
    if TOOL_SPECS[name].tool_class is ToolClass.READ:
        assert method == "GET" or name in READ_POSTS, f"READ tool {name} used {method}"


def _cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def test_every_tool_has_an_eval_case():
    expected = {t for c in _cases() for t in c["expect_tools"]}
    missing = set(TOOL_SPECS) - expected
    assert not missing, f"tools with no eval case: {sorted(missing)}"


def test_every_starter_has_an_eval_case():
    """No exceptions list: the five G1 gaps this test was born tracking are closed.

    Task 5.3 backed the insights chip with list_stage_models, and Task 5.6 replaced the
    initiatives/projects chips that no tool could serve. The tracking set that stood in
    for them is deleted rather than left empty — an empty allow-list is the same
    assertion with a stale comment attached. Task 6.5 restores section-specific chips on
    top of Pack P, and it has to add their eval cases in the same change to stay green.
    """
    src = Path("web/src/features/assistant/starters.ts").read_text()
    chips = set(re.findall(r"^\s+'([^']+)',\s*$", src, re.M))
    prompts = {c["prompt"] for c in _cases()}
    missing = chips - prompts
    assert not missing, f"starter chips with no eval case: {sorted(missing)}"
