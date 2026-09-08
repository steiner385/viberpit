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
from server.assistant.tools import OPERATOR_TOOLS, TOOL_SPECS, ToolClass, build_tools

CASES = Path("docs/superpowers/evals/assistant/cases.jsonl")
FEATURE_DOC = Path("docs/product/features/ai-assistant.md")
UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

# Minimal valid args per tool so every handler reaches its HTTP call. A tool missing here
# fails loudly (KeyError) — add its args when you add the tool.
ARGS: dict[str, dict] = {
    "list_sessions": {}, "get_session": {"id": UUID}, "get_session_transcript": {"id": UUID},
    "get_tool_result": {"id": UUID, "tool_id": "c1"}, "export_session": {"id": UUID},
    "search_sessions": {"query": "x"}, "list_archived_sessions": {}, "list_accounts": {},
    "list_hosts": {}, "list_prs": {}, "get_pr": {"repo": "o/n", "number": 1},
    "get_pipeline_health": {"repo": "o/n"},
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
    "archive_sessions": {"ids": [UUID], "titles": ["A"]},
    "unarchive_sessions": {"ids": [UUID], "titles": ["A"]},
    "resume_sessions": {"ids": [UUID], "titles": ["A"]},
    "rotate_sessions": {"ids": [UUID], "titles": ["A"]},
    "delete_sessions": {"ids": [UUID], "titles": ["A"]},
    "set_autopilot_paused": {"id": UUID, "paused": True}, "fix_pr": {"id": UUID, "number": 1},
    "comment_on_pr": {"repo": "o/n", "number": 1, "body": "b"},
    "create_issue": {"repo": "o/n", "title": "t", "body": "b"},
    "set_automation_enabled": {"id": "autopilot", "enabled": False},
    "create_notification_rule": {"event_class": "session.done", "action": "push"},
    "delete_notification_rule": {"id": "rule-1"},
    "watch": {"kind": "pr", "target": "o/n", "condition": "breaks"},
    "list_watches": {}, "delete_watch": {"id": "watch-1"},
    "set_quiet_hours": {"start": "22:00"},
    "delete_session": {"id": UUID}, "cancel_queued_session": {"id": UUID},
    "ready_merge_pr": {"repo": "o/n", "number": 1},
    "list_initiatives": {"status": "build"}, "get_initiative": {"slug": "a"},
    "advance_initiative": {"slug": "a"},
    "list_products": {}, "get_product": {"slug": "a"}, "get_product_deps": {"slug": "a"},
    "get_priority_queue": {"min_status": "ready"}, "list_triage": {},
    "get_onboarding_status": {"slug": "a"},
    "get_automation_actionable": {"id": "pr-doctor"},
    "list_automation_sessions": {"id": "autopilot"},
    "list_backlog_candidates": {}, "get_backlog_groups": {}, "get_cicd_findings": {},
    "get_pr_doctor_consolidation": {}, "get_pr_doctor_job": {"job_id": "prd-1a2b3c4d"},
    "preview_schedule": {"schedule": {"cron": "0 8 * * *"}},
    "list_presets": {}, "list_templates": {},
    "render_template": {"id": "t1", "vars": {"name": "x"}},
    "list_permission_profiles": {},
    "trigger_automation_action": {"id": "pr-doctor", "item_id": "acme/checkout#10",
                                  "kind": "failing_ci"},
    "dispatch_backlog": {"refs": ["acme/billing#18"]},
    "consolidate_prs": {"repo": "acme/billing", "numbers": [42, 43]},
    "resolve_triage": {"proposal_id": "p1", "decision": "accept"},
    "request_product_sync": {"slug": "a"},
    "start_deploy": {"ref": "abc123"}, "upgrade_host": {"name": "rocky"},
    "refresh_account": {"name": "a1"},
    "set_account_enabled": {"name": "a1", "enabled": False},
    "start_reauth": {"name": "a1"},
    "list_adoptable_sessions": {}, "suggest_next_prompt": {"id": UUID},
    "rename_session": {"id": UUID, "name": "New name"},
    "move_in_queue": {"id": UUID, "op": "top"},
    "pin_account": {"id": UUID, "pinned": True},
    "rotate_session": {"id": UUID}, "reload_session": {"id": UUID},
    "adopt_session": {"claude_session_id": "ext-1"},
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
    tools = build_tools(_recording_api(seen), operator=True)
    tool = next(t for t in tools if t.name == name)
    await tool.handler(ARGS[name])
    if name == "report_gap":
        # The sole exemption: the gap it records IS that no endpoint exists, so it
        # must reach the API zero times. Asserted rather than skipped.
        assert seen == [], "report_gap must not call the API"
        return
    assert len(seen) == 1, f"{name} made {len(seen)} calls; a tool is ONE endpoint"
    method, path = seen[0]
    assert route_allowed(method, path, operator=(name in OPERATOR_TOOLS)), (
        f"{name} hit {method} {path}, not allowlisted")
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


# The feature doc's "Tool surface (v2)" bullet: the headline totals, then one table row
# per pack (`| Pack A — automations control | 11 | … |`). The header and rule rows can't
# match because column two must be digits.
_DOC_TOTALS = re.compile(r"\*\*Tool surface \(v2\)\*\* — (\d+) tools are registered.*?"
                         r"(\d+) are exposed by default")
_DOC_PACK_ROW = re.compile(r"^ *\| *([^|]+?) *\| *(\d+) *\|", re.M)


def _tool_surface_section() -> str:
    """The Tool surface bullet only, so sibling bullets' tables can't be miscounted."""
    text = FEATURE_DOC.read_text()
    start = text.index("**Tool surface (v2)**")
    end = text.find("\n- **", start)
    return text[start:end if end != -1 else len(text)]


def test_feature_doc_tool_counts_match_the_registry():
    """The feature doc's pack table is a hand-copy of TOOL_SPECS, so the code checks it.

    Task 6.5 wrote these counts as prose ("68 tools ... 35 base ... Packs S, C, G, N and X
    are not yet built") and they were wrong within the day: Packs S and N landed beside it
    and the registry reached 84. A number describing a list that lives in code has to be
    verified by that code — otherwise the next reader is the only thing standing between a
    stale doc and a wrong one, and this is the doc the assistant's own operators read.
    """
    section = _tool_surface_section()
    # Totals match against a whitespace-collapsed copy so a prose line wrap can't fail the
    # gate; the pack rows keep the raw text, since their regex is line-anchored.
    totals = _DOC_TOTALS.search(" ".join(section.split()))
    assert totals, "the Tool surface bullet must state 'N tools are registered' and 'M are exposed by default'"
    registered, exposed = (int(n) for n in totals.groups())
    packs = {m.group(1): int(m.group(2)) for m in _DOC_PACK_ROW.finditer(section)}

    assert registered == len(TOOL_SPECS), (
        f"doc says {registered} tools registered; TOOL_SPECS has {len(TOOL_SPECS)}")
    assert exposed == len(build_tools(_recording_api([]))), (
        f"doc says {exposed} exposed by default; build_tools() returns "
        f"{len(build_tools(_recording_api([])))} with the operator tier off")
    assert sum(packs.values()) == len(TOOL_SPECS), (
        f"pack rows sum to {sum(packs.values())}, registry has {len(TOOL_SPECS)} — every "
        f"tool belongs to exactly one pack row, so a new tool must land in one")
    operator_rows = [v for k, v in packs.items() if "operator" in k.lower()]
    assert operator_rows == [len(OPERATOR_TOOLS)], (
        f"the operator-tier row must count OPERATOR_TOOLS ({len(OPERATOR_TOOLS)}), got {operator_rows}")
