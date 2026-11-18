"""update_product_repos / map_moved_issues: one route each, operator tier, honest cards."""
from __future__ import annotations

import json

import httpx
import pytest

from server.assistant.policy import route_allowed
from server.assistant.tools import (MCP_PREFIX, OPERATOR_TOOLS, TOOL_SPECS, ToolClass,
                                    batch_items, build_tools)
from server.tests.test_assistant_tools import _api, _tool


def _tools(handler):
    return build_tools(_api(handler), operator=True)


def _record(seen: list):
    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"slug": "kindash"})
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("args,route,body", [
    ({"product": "kindash", "action": "add", "repo": "o/r", "makePrimary": True},
     "/api/pm/projects/kindash/repos", {"repo": "o/r", "makePrimary": True}),
    ({"product": "kindash", "action": "add", "repo": "o/r"},
     "/api/pm/projects/kindash/repos", {"repo": "o/r", "makePrimary": False}),
    ({"product": "kindash", "action": "set_primary", "repo": "o/r", "expectedPrimary": "o/p"},
     "/api/pm/projects/kindash/repos/o/r/primary", {"expectedPrimary": "o/p"}),
    ({"product": "kindash", "action": "retire", "repo": "o/p", "expectedPrimary": "o/p",
      "newPrimary": "o/r", "reason": "moved", "supersededBy": "o/r"},
     "/api/pm/projects/kindash/repos/o/p/retire",
     {"reason": "moved", "supersededBy": "o/r", "newPrimary": "o/r", "expectedPrimary": "o/p"}),
    ({"product": "kindash", "action": "unretire", "repo": "o/r"},
     "/api/pm/projects/kindash/repos/o/r/unretire", None),
])
async def test_update_product_repos_hits_exactly_one_route(args, route, body):
    seen: list = []
    out = await _tool(_tools(_record(seen)), "update_product_repos").handler(args)
    assert out.get("is_error") is not True
    assert seen == [("POST", route, body)]
    assert route_allowed("POST", route, operator=True) and not route_allowed("POST", route)


@pytest.mark.asyncio
@pytest.mark.parametrize("args,msg", [
    ({"product": "kindash", "action": "retire", "repo": "o/p"}, "expectedPrimary"),
    ({"product": "kindash", "action": "set_primary", "repo": "o/p"}, "expectedPrimary"),
    ({"product": "kindash", "action": "delete", "repo": "o/p"}, "action"),
    ({"product": "kindash", "action": "add", "repo": "not a repo"}, "owner/name"),
    ({"product": "Bad Slug", "action": "add", "repo": "o/r"}, "product"),
])
async def test_update_product_repos_refuses_locally(args, msg):
    seen: list = []
    out = await _tool(_tools(_record(seen)), "update_product_repos").handler(args)
    assert out["is_error"] is True and seen == []
    assert msg in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_update_product_repos_relays_a_server_refusal():
    def handler(req):
        return httpx.Response(409, json={"detail": {"error": "primary_changed",
                                                    "message": "primary is o/x, not o/p"}})
    out = await _tool(_tools(handler), "update_product_repos").handler(
        {"product": "kindash", "action": "set_primary", "repo": "o/r", "expectedPrimary": "o/p"})
    assert out["is_error"] is True and "primary is o/x" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_map_moved_issues_posts_the_pairs():
    seen: list = []
    pairs = [{"old": "o/old#1", "new": "o/new#2"}]
    out = await _tool(_tools(_record(seen)), "map_moved_issues").handler(
        {"product": "kindash", "pairs": pairs})
    assert out.get("is_error") is not True
    assert seen == [("POST", "/api/pm/projects/kindash/moved-refs", {"pairs": pairs})]
    assert route_allowed("POST", "/api/pm/projects/kindash/moved-refs", operator=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("pairs", [[], "x", [{"old": "a"}], [{"old": 1, "new": 2}]])
async def test_map_moved_issues_refuses_bad_pairs(pairs):
    seen: list = []
    out = await _tool(_tools(_record(seen)), "map_moved_issues").handler(
        {"product": "kindash", "pairs": pairs})
    assert out["is_error"] is True and seen == []


def test_both_are_operator_tier_mutations():
    for name in ("update_product_repos", "map_moved_issues"):
        assert TOOL_SPECS[name].tool_class is ToolClass.MUTATION
        assert name in OPERATOR_TOOLS
        assert name not in {t.name for t in build_tools(_api(lambda r: httpx.Response(200)))}


def test_repo_card_shows_the_primary_move():
    def items(**kw):
        return batch_items(MCP_PREFIX + "update_product_repos", kw)
    assert items(action="retire", repo="o/p", expectedPrimary="o/p", newPrimary="o/r",
                 reason="moved", supersededBy="o/r") == [
        "retire o/p", "primary: o/p → o/r", "reason: moved", "supersededBy: o/r"]
    # a retire of a non-primary leaves the primary unchanged
    assert items(action="retire", repo="o/x", expectedPrimary="o/p") == [
        "retire o/x", "primary: o/p → o/p"]
    assert items(action="set_primary", repo="o/r", expectedPrimary="o/p") == [
        "set_primary o/r", "primary: o/p → o/r"]
    assert items(action="add", repo="o/r", makePrimary=True, expectedPrimary="o/p")[1] == (
        "primary: o/p → o/r")
    # primary is not part of an un-flagged add or an unretire: no row.
    assert items(action="add", repo="o/r", expectedPrimary="o/p") == ["add o/r"]
    assert items(action="unretire", repo="o/r") == ["unretire o/r"]
    assert items(action=1) == []


def test_moved_card_lists_each_pair():
    assert batch_items(MCP_PREFIX + "map_moved_issues", {"pairs": [
        {"old": "o/a#1", "new": "o/b#2"}, {"old": "bad"}, "x"]}) == ["o/a#1 → o/b#2"]
    assert batch_items(MCP_PREFIX + "map_moved_issues", {"pairs": "x"}) == []
