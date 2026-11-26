"""#3527: "Always allow" on the assistant's confirmation cards.

Which of the app's own MCP tools a standing rule may answer is decided by ONE list,
``NEVER_RULED_TOOLS``: a mutating tool on it always asks a person and its card never
offers a standing grant; every other mutating tool (DESTRUCTIVE included — close_issue
is the issue's headline case) is eligible. The rules themselves stay human-only: that
half is pinned in test_permission_rules_api.py (``test_assistant_token_cannot_grant_or_edit``).
"""
import asyncio

import pytest
from sqlalchemy import select

from server.assistant.tools import OPERATOR_TOOLS
from server.db import db_session
from server.db.models import AuditEvent
from server.mcp_spec import ToolClass, surfaces
from server.permission_rules import store
from server.permission_rules.gate import StandingRuleGate
from server.permission_rules.patterns import NEVER_RULED_TOOLS
from server.permission_rules.safety import assess
from server.sessions.permissions import PermissionRegistry
from viberpit.brand import MCP_TOOL_PREFIX, MCP_TOOL_PREFIXES

# The issue's list, verbatim: irreversible or high-blast-radius actions keep their card.
ISSUE_NEVER_LIST = frozenset({
    "start_deploy", "upgrade_host", "delete_session", "delete_sessions", "consolidate_prs",
    "dispatch_backlog", "trigger_automation_action", "ready_merge_pr",
    # account enable/disable/reauth
    "set_account_enabled", "start_reauth",
    # product policy/repo changes
    "update_product_policy", "update_product_repos",
})

# DESTRUCTIVE tools reviewed as reversible enough for a standing rule (#3527). A new
# DESTRUCTIVE tool must be added here or to NEVER_RULED_TOOLS: the ratchet below fails
# otherwise, so the choice is never made by default.
GRANTABLE_DESTRUCTIVE = frozenset({
    "close_issue", "cancel_queued_session", "file_feedback_cluster",
    "dismiss_feedback_cluster", "attach_feedback_to_issue", "attach_feedback_to_session",
})

S1, S2 = "asst-1", "asst-2"
FACTS = {S1: (None, "/scratch/a"), S2: (None, "/scratch/b")}


def _never(stem: str) -> bool:
    return all(f"{p}{stem}" in NEVER_RULED_TOOLS for p in MCP_TOOL_PREFIXES)


@pytest.mark.parametrize("stem", sorted(ISSUE_NEVER_LIST | OPERATOR_TOOLS))
def test_never_list_and_operator_tier_are_never_ruled_under_both_prefixes(stem):
    assert _never(stem), f"{stem} must never be auto-allowable"


def test_every_destructive_tool_is_classified():
    destructive = {name for table in surfaces().values() for name, spec in table.items()
                   if spec.tool_class is ToolClass.DESTRUCTIVE}
    unclassified = {n for n in destructive if not _never(n) and n not in GRANTABLE_DESTRUCTIVE}
    assert not unclassified, (
        f"classify {sorted(unclassified)}: add to NEVER_RULED_TOOLS or GRANTABLE_DESTRUCTIVE")
    assert not {n for n in GRANTABLE_DESTRUCTIVE if _never(n)}


@pytest.mark.parametrize("stem", sorted(ISSUE_NEVER_LIST | OPERATOR_TOOLS))
def test_a_rule_naming_a_never_list_tool_is_refused(stem):
    for scope in ("session", "global"):
        assert assess(MCP_TOOL_PREFIX + stem, scope).level == "refuse"


# ── enforcement ─────────────────────────────────────────────────────────────────────

pytestmark_db = pytest.mark.usefixtures("isolated_db")


def _reg():
    reg = PermissionRegistry()
    reg.rule_gate = StandingRuleGate(FACTS.get)
    return reg


async def _ask(reg, sid, stem, inp=None, mode="interactive"):
    return await reg.request(sid, "t", MCP_TOOL_PREFIX + stem, inp or {}, mode=mode,
                             denylist=PermissionRegistry.DANGEROUS, identity="me")


async def _add(scope, key, stem):
    async with db_session() as db:
        r = await store.add_rule(db, scope=scope, scope_key=key, pattern=MCP_TOOL_PREFIX + stem,
                                 created_by="me", created_via=None)
        await db.commit()
        return r


@pytestmark_db
@pytest.mark.parametrize("stem", ["close_issue", "add_issue_labels"])
def test_eligible_card_offers_always_allow_with_the_tool_rule(stem):
    async def go():
        reg = _reg()
        pid, fut, always_allowed = await _ask(reg, S1, stem, mode="full-auto")
        assert pid is not None, "a mutating app tool still cards in every mode"
        assert always_allowed is True
        assert [s["pattern"] for s in reg.suggestions_for(pid)
                if s["level"] == "ok"] == [MCP_TOOL_PREFIX + stem]
        assert reg.session_grant_effective_for(pid) is True
        reg.resolve(pid, "deny", "me")
    asyncio.run(go())


@pytestmark_db
@pytest.mark.parametrize("stem", ["start_deploy", "delete_sessions", "set_account_enabled",
                                  "update_product_repos", "resolve_triage"])
def test_never_list_card_offers_nothing_and_ignores_a_stored_rule(stem):
    async def go():
        # Stored behind the safety check's back: enforcement must not trust the store.
        await _add("global", None, stem)
        reg = _reg()
        pid, _fut, always_allowed = await _ask(reg, S1, stem)
        assert pid is not None, f"{stem} must always ask a person"
        assert always_allowed is False
        assert reg.suggestions_for(pid) == []
        reg.resolve(pid, "deny", "me")
    asyncio.run(go())


@pytestmark_db
def test_conversation_rule_skips_the_card_only_where_it_matches():
    async def go():
        await _add("session", S1, "close_issue")
        reg = _reg()
        pid, fut, _ = await _ask(reg, S1, "close_issue", {"refs": ["o/r#1"]})
        assert pid is None and fut.result() is True
        other, _, _ = await _ask(reg, S1, "comment_on_issue")
        assert other is not None, "a different tool still asks"
        later, _, _ = await _ask(reg, S2, "close_issue")
        assert later is not None, "a conversation rule does not reach another conversation"
        for p in (other, later):
            reg.resolve(p, "deny", "me")
    asyncio.run(go())


@pytestmark_db
def test_always_rule_covers_every_conversation_and_revoking_takes_effect_next_call():
    async def go():
        r = await _add("global", None, "close_issue")
        reg = _reg()
        for sid in (S1, S2):
            pid, fut, _ = await _ask(reg, sid, "close_issue")
            assert pid is None and fut.result() is True
        async with db_session() as db:
            await store.revoke_rule(db, r.id)
            await db.commit()
        pid, _, _ = await _ask(reg, S1, "close_issue")
        assert pid is not None
        reg.resolve(pid, "deny", "me")
    asyncio.run(go())


@pytestmark_db
def test_auto_allowed_call_is_audited_with_the_rule():
    async def go():
        r = await _add("session", S1, "close_issue")
        reg = _reg()
        await _ask(reg, S1, "close_issue")
        async with db_session() as db:
            rows = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "permission.rule_allow"))).scalars().all()
        assert len(rows) == 1
        detail = rows[0].detail
        assert detail["ruleId"] == r.id
        assert detail["pattern"] == MCP_TOOL_PREFIX + "close_issue"
        assert detail["tool"] == MCP_TOOL_PREFIX + "close_issue"
        assert detail["sessionId"] == S1
    asyncio.run(go())
