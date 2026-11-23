"""create_product (#3433): the Products page's own create, operator tier, honest card."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server.assistant.policy import route_allowed
from server.assistant.tools import (MCP_PREFIX, NO_SESSION_TARGET, OPERATOR_TOOLS,
                                    TOOL_SPECS, ToolClass, batch_items, build_tools)
from server.tests.test_assistant_tools import _api, _tool
from server.tests.pm_ownership_fixtures import identity_repositories
from server.tests.test_pm_api import IDENTITY_HEADERS, _client


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories",
                        identity_repositories)


def _tools(handler):
    return build_tools(_api(handler), operator=True)


def _record(seen: list):
    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(201, json={
            "slug": "keel", "name": "Keel", "onboardingState": "registered",
            "repos": [{"repo": "steiner385/keel", "isPrimary": True, "backfillDone": False}]})
    return handler


@pytest.mark.asyncio
async def test_posts_one_create_with_the_primary_first_and_a_derived_slug():
    seen: list = []
    out = await _tool(_tools(_record(seen)), "create_product").handler(
        {"name": " Keel ", "repo": "steiner385/keel",
         "repos": ["steiner385/keel-docs", "steiner385/keel", "steiner385/keel-docs"],
         "description": "Boats"})
    assert out.get("is_error") is not True
    assert seen == [("POST", "/api/pm/projects", {
        "name": "Keel", "slug": "keel", "description": "Boats",
        "repos": [{"repo": "steiner385/keel", "isPrimary": True},
                  {"repo": "steiner385/keel-docs"}]})]
    result = json.loads(out["content"][0]["text"])["data"]
    assert result["slug"] == "keel" and result["onboardingState"] == "registered"
    assert route_allowed("POST", "/api/pm/projects", operator=True)
    assert not route_allowed("POST", "/api/pm/projects")


@pytest.mark.asyncio
@pytest.mark.parametrize("args,msg", [
    ({"repo": "o/r"}, "name"),
    ({"name": "  ", "repo": "o/r"}, "name"),
    ({"name": "Keel"}, "owner/name"),
    ({"name": "Keel", "repo": "not a repo"}, "owner/name"),
    ({"name": "Keel", "repo": "o/r", "repos": ["bad"]}, "repos"),
    ({"name": "Keel", "repo": "o/r", "repos": [f"o/r{i}" for i in range(20)]}, "repos"),
    ({"name": "Keel", "repo": "o/r", "slug": "Bad Slug"}, "slug"),
    ({"name": "x" * 80, "repo": "o/r"}, "slug"),
])
async def test_refuses_locally(args, msg):
    seen: list = []
    out = await _tool(_tools(_record(seen)), "create_product").handler(args)
    assert out["is_error"] is True and seen == []
    assert msg in out["content"][0]["text"]


def test_is_an_operator_tier_mutation_outside_the_session_guard():
    assert TOOL_SPECS["create_product"].tool_class is ToolClass.MUTATION
    assert "create_product" in OPERATOR_TOOLS and "create_product" in NO_SESSION_TARGET
    assert "create_product" not in {t.name for t in build_tools(_api(lambda r: httpx.Response(200)))}


def test_card_lists_name_slug_and_repos_as_sent():
    def items(**kw):
        return batch_items(MCP_PREFIX + "create_product", kw)
    assert items(name="Keel Boats", repo="o/keel", repos=["o/docs"]) == [
        "name: Keel Boats", "slug: keel-boats", "primary: o/keel", "repo: o/docs"]
    assert items(name="Keel", slug="boat", repo="o/keel", description="d") == [
        "name: Keel", "slug: boat", "primary: o/keel", "description: d"]
    # malformed input still raises a card, just with no rows to misread
    assert items(name="Keel") == []


def _through_the_real_endpoint(client):
    """Route the tool's loopback calls into the app, as the Products page's create."""
    def handler(req: httpx.Request) -> httpx.Response:
        resp = client.request(req.method, req.url.path, content=req.content,
                              headers={**IDENTITY_HEADERS, "content-type": "application/json"})
        return httpx.Response(resp.status_code, content=resp.content,
                              headers={"content-type": "application/json"})
    return _tools(handler)


def test_registers_and_starts_onboarding_through_the_products_page_create():
    with _client() as c:
        tools = _through_the_real_endpoint(c)
        create = _tool(tools, "create_product").handler

        out = asyncio.run(create({"name": "Keel", "repo": "steiner385/keel"}))
        assert out.get("is_error") is not True, out
        assert json.loads(out["content"][0]["text"])["data"] == {
            "slug": "keel", "name": "Keel", "onboardingState": "registered",
            "repos": [{"repo": "steiner385/keel", "isPrimary": True}]}
        onboarding = c.get("/api/pm/projects/keel/onboarding", headers=IDENTITY_HEADERS)
        assert onboarding.status_code == 200 and onboarding.json()["state"] == "registered"

        # the slug is taken
        out = asyncio.run(create({"name": "Keel", "repo": "steiner385/other"}))
        assert out["is_error"] is True and "already exists" in out["content"][0]["text"]

        # the repo already belongs to an active product
        out = asyncio.run(create({"name": "Keel Two", "repo": "steiner385/keel"}))
        assert out["is_error"] is True and "ownership_conflict" in out["content"][0]["text"]
        assert c.get("/api/pm/projects/keel-two", headers=IDENTITY_HEADERS).status_code == 404
