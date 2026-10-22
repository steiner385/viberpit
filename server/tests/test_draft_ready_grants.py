"""The user-approved path past the draft-PR guard (cairnea/KinDash#15268): a session
asks with ``request_draft_ready``, a human allows the card, the registry records a
grant for that one PR in that one session, and ``gh pr ready`` on it passes once.
Nothing the session itself does can write the grant."""
from __future__ import annotations

import json

import pytest

import server.sessions.tools  # noqa: F401 - registers the surface
from server.mcp_spec import MCP_PREFIX
from server.sessions.draft_hold_guard import (
    MUTATION_REASON, REQUEST_TOOL, DraftReadyGrants, TargetPr, draft_ready_guard,
    not_own_reason, parse_ready_request)
from server.sessions.permissions import PermissionRegistry
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.tools import build_session_tools

TOOL = MCP_PREFIX + REQUEST_TOOL


@pytest.fixture(autouse=True)
def _no_inherited_gh_repo(monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)


def _call(command, cwd="/w/s"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _resolve(call, cwd):
    return TargetPr(call.repo or "o/r", int(call.selector or 17), "feat/x", True)


async def _never_own(repo, number, head_ref):
    return False


def _denied(out):
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    return spec["permissionDecisionReason"]


async def _approve(reg, session_id, repo, number, *, decision="allow", identity="u",
                   mode="full-auto"):
    """Raise the card the way the SDK's can_use_tool would, then answer it."""
    pid, fut, always = await reg.request(
        session_id, "t1", TOOL, {"repo": repo, "number": number, "reason": "user asked"},
        mode=mode, denylist=(), identity="u")
    assert pid is not None and not fut.done(), "the request must raise a card"
    assert always is False, "the card must never offer Always allow"
    return reg.resolve(pid, decision, identity), fut


def _guard(reg, session_id="s1"):
    return draft_ready_guard(_never_own, _resolve,
                             grants=reg.draft_ready_grants.for_session(session_id))


@pytest.mark.asyncio
async def test_without_a_grant_the_deny_says_how_to_ask():
    reason = _denied(await _guard(PermissionRegistry())(_call("gh pr ready 17 --repo o/r"),
                                                          "tu", None))
    assert reason == not_own_reason("o/r", 17)
    assert f"`{REQUEST_TOOL}`" in reason and 'repo="o/r", number=17' in reason
    assert "gh pr ready 17 --repo o/r" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "auto", "allowlist", "acceptEdits",
                                  "full-auto"])
async def test_the_request_raises_a_card_in_every_mode(mode):
    reg = PermissionRegistry()
    ok, fut = await _approve(reg, "s1", "o/r", 17, mode=mode)
    assert ok and fut.result() is True


@pytest.mark.asyncio
async def test_an_allowed_card_grants_that_pr_once():
    reg = PermissionRegistry()
    hook = _guard(reg)
    await _approve(reg, "s1", "O/R", 17)
    assert await hook(_call("gh pr ready 17 --repo o/r"), "tu", None) == {}
    # Spent: a draft the owner converts back is a new hold.
    assert _denied(await hook(_call("gh pr ready 17 --repo o/r"), "tu", None))


@pytest.mark.asyncio
async def test_a_grant_for_one_pr_never_covers_another():
    reg = PermissionRegistry()
    hook = _guard(reg)
    await _approve(reg, "s1", "o/r", 17)
    assert _denied(await hook(_call("gh pr ready 18 --repo o/r"), "tu", None)) \
        == not_own_reason("o/r", 18)
    assert _denied(await hook(_call("gh pr ready 17 --repo o/other"), "tu", None))
    # A chain with an ungranted sibling is denied whole and does not spend the grant.
    assert _denied(await hook(_call("gh pr ready 17 -R o/r && gh pr ready 18 -R o/r"),
                              "tu", None))
    assert await hook(_call("gh pr ready 17 --repo o/r"), "tu", None) == {}


@pytest.mark.asyncio
async def test_a_grant_belongs_to_the_session_that_asked():
    reg = PermissionRegistry()
    await _approve(reg, "s1", "o/r", 17)
    assert _denied(await _guard(reg, "s2")(_call("gh pr ready 17 --repo o/r"), "tu", None))


@pytest.mark.asyncio
async def test_a_denied_or_foreign_decision_grants_nothing():
    reg = PermissionRegistry()
    hook = _guard(reg)
    await _approve(reg, "s1", "o/r", 17, decision="deny")
    ok, fut = await _approve(reg, "s1", "o/r", 17, identity="someone-else")
    assert ok is False and not fut.done()
    assert _denied(await hook(_call("gh pr ready 17 --repo o/r"), "tu", None))


@pytest.mark.asyncio
async def test_allowing_some_other_tool_card_grants_nothing():
    reg = PermissionRegistry()
    pid, _, _ = await reg.request("s1", "t1", "Bash",
                                  {"command": "gh pr ready 17", "repo": "o/r", "number": 17},
                                  mode="interactive", denylist=(), identity="u")
    reg.resolve(pid, "allow", "u")
    assert not reg.draft_ready_grants.has("s1", "o/r", 17)


@pytest.mark.asyncio
async def test_the_mutation_stays_refused_even_with_a_grant():
    reg = PermissionRegistry()
    await _approve(reg, "s1", "o/r", 17)
    out = await _guard(reg)(_call(
        "gh api graphql -f query='mutation{markPullRequestReadyForReview(input:"
        "{pullRequestId:\"PR_x\"}){clientMutationId}}'"), "tu", None)
    assert _denied(out) == MUTATION_REASON


@pytest.mark.asyncio
async def test_own_pr_undo_and_non_draft_paths_are_unchanged():
    async def owns(repo, number, head_ref):
        return (repo, number) == ("o/r", 17)
    grants = DraftReadyGrants().for_session("s1")
    hook = draft_ready_guard(owns, _resolve, grants=grants)
    assert await hook(_call("gh pr ready 17 --repo o/r"), "tu", None) == {}
    assert await hook(_call("gh pr ready 18 --repo o/r --undo"), "tu", None) == {}

    def ready(call, cwd):
        return TargetPr("o/r", 18, "feat/y", False)
    assert await draft_ready_guard(_never_own, ready, grants=grants)(
        _call("gh pr ready 18 --repo o/r"), "tu", None) == {}


def _ctx(granted):
    return SessionToolContext(session_id="s1", identity="u", kind="coding",
                              engine="claude_code", record=lambda: None,
                              signalling_tools=True, draft_ready_granted=granted)


async def _tool(ctx, args):
    tool = next(t for t in build_session_tools(ctx) if t.name == REQUEST_TOOL)
    return await tool.handler(args)


@pytest.mark.asyncio
async def test_the_tool_reports_the_grant_but_never_writes_it():
    """A session calling the handler without a card (bypassPermissions skips cards)
    gets granted=false, and the guard still denies: the handler only reads."""
    reg = PermissionRegistry()
    view = reg.draft_ready_grants.for_session("s1")
    out = await _tool(_ctx(view.has), {"repo": "o/r", "number": 17, "reason": "asked"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["granted"] is False and "hand back" in data["note"]
    assert _denied(await _guard(reg)(_call("gh pr ready 17 --repo o/r"), "tu", None))

    await _approve(reg, "s1", "o/r", 17)
    out = await _tool(_ctx(view.has), {"repo": "o/r", "number": 17, "reason": "asked"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert data == {"granted": True, "repo": "o/r", "number": 17,
                    "next": "gh pr ready 17 --repo o/r"}


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"repo": "o/r", "number": 17},                       # no reason
    {"repo": "not-a-repo", "number": 17, "reason": "x"},
    {"repo": "o/r", "number": 0, "reason": "x"},
    {"repo": "o/r", "number": True, "reason": "x"},
])
async def test_the_tool_rejects_input_that_names_no_single_pr(args):
    out = await _tool(_ctx(lambda r, n: True), args)
    assert out.get("is_error") is True


def test_parse_ready_request():
    assert parse_ready_request({"repo": " o/r ", "number": "#17"}) == ("o/r", 17)
    assert parse_ready_request({"repo": "o/r/x", "number": 17}) is None
    assert parse_ready_request("o/r#17") is None
