"""The Codex vendor command/skill catalog, end to end over the real transport.

#1286: `CodexAgentDriver.server_info()` used to return None, so a Codex session's
command palette held ccswitch's one hardcoded synthetic command and nothing else
— no vendor half, and `/model` dispatchable while invisible. These tests pin the
two properties that make the replacement *truthful* rather than merely populated:

* every catalog entry is a skill the app-server just reported, at a path it
  resolved — nothing is synthesized, and a read failure says "no answer" rather
  than "no skills";
* dispatching one puts a native `SkillUserInput` on the wire, NOT a `/name` text
  input. Codex does not parse slash syntax, so forwarding Claude's would reach
  the model as literal text.

They run through the same real driver → manager → protocol → JSONL → fake
app-server stack as `test_codex_driver.py`, so what is asserted is the frame that
actually goes out, not the driver's opinion of it.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from server.codex import schema
from server.codex.capabilities import CODEX_CAPABILITIES
from server.sessions.commands import dispatcher as cmd_dispatcher
from server.sessions.commands.catalog import build_catalog
from server.tests.test_codex_driver import Harness


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


def skill(name: str, path: str, *, description: str = "", **extra) -> dict:
    body = {
        "name": name,
        "path": path,
        "description": description or f"{name} description",
        "enabled": True,
        "scope": "repo",
    }
    body.update(extra)
    return body


def skills_result(*skills: dict, cwd: str = "/work", errors: list | None = None) -> dict:
    return {"data": [{"cwd": cwd, "skills": list(skills), "errors": errors or []}]}


# ---------------------------------------------------------------------------
# The vendor half
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_info_publishes_the_app_server_s_own_skills(harness):
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(
            skill("pr-review", "/work/.codex/skills/pr-review"),
            skill("changelog", "/work/.codex/skills/changelog", description="Draft one"),
        ),
    )

    info = await driver.server_info()

    assert info == {
        "commands": [
            {"name": "pr-review", "description": "pr-review description"},
            {"name": "changelog", "description": "Draft one"},
        ]
    }
    # The read names this session's cwd rather than relying on the upstream
    # "current session" default: one app-server serves every session on an
    # account, so it has no single current session to resolve.
    assert harness.params(schema.METHOD_SKILLS_LIST)["cwds"] == [driver.cwd]


@pytest.mark.asyncio
async def test_a_disabled_skill_is_never_offered(harness):
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(
            skill("on", "/work/skills/on"),
            skill("off", "/work/skills/off", enabled=False),
        ),
    )

    info = await driver.server_info()

    assert [c["name"] for c in info["commands"]] == ["on"]


@pytest.mark.asyncio
async def test_a_skill_with_no_path_is_dropped_rather_than_offered(harness):
    """`turn/start` identifies a skill by name AND path, so a pathless entry
    could only ever fail a turn. Dropping the one entry (rather than raising)
    keeps the rest of the catalog usable."""
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(
            {"name": "broken", "description": "d", "enabled": True, "scope": "repo"},
            skill("fine", "/work/skills/fine"),
        ),
    )

    info = await driver.server_info()

    assert [c["name"] for c in info["commands"]] == ["fine"]


@pytest.mark.asyncio
async def test_a_failed_read_answers_none_not_an_empty_catalog(harness):
    """None is what the orchestrator keys its last-known-catalog fallback off.
    An empty list would mean "this project has no skills", which is a different
    fact and would blank a working palette on one transient failure."""
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        error={"code": -32603, "message": "skills scan failed"},
    )

    assert await driver.server_info() is None


@pytest.mark.asyncio
async def test_the_catalog_offers_skills_alongside_the_app_owned_commands(harness):
    """The whole point: the palette shows what the ENGINE publishes and what
    CCSWITCH implements, each labelled with who owns it."""
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(skill("pr-review", "/work/skills/pr-review")),
    )

    catalog = build_catalog(await driver.server_info(), driver.capabilities)
    by = {c.name: c for c in catalog}

    assert by["pr-review"].kind == "skill" and by["pr-review"].source == "vendor"
    assert by["model"].source == "app"
    assert by["account"].source == "app"
    assert not any(c.kind == "passthrough" for c in catalog)


# ---------------------------------------------------------------------------
# Live dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatching_a_skill_sends_a_native_skill_input(harness):
    """The acceptance criterion in one assertion: what reaches `turn/start` is
    the app-server's own skill-invocation variant, not `/pr-review` as text."""
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(skill("pr-review", "/work/skills/pr-review")),
    )
    cmd = next(
        c
        for c in build_catalog(await driver.server_info(), driver.capabilities)
        if c.name == "pr-review"
    )

    result = await cmd_dispatcher.dispatch(driver, cmd, "")
    await harness.settle()

    assert result is None, "a skill streams as a normal turn"
    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "skill", "name": "pr-review", "path": "/work/skills/pr-review"}
    ]


@pytest.mark.asyncio
async def test_skill_arguments_ride_along_as_text_in_the_same_turn(harness):
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(skill("pr-review", "/work/skills/pr-review")),
    )
    await driver.server_info()

    await driver.send_skill("pr-review", "PR #42")
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "skill", "name": "pr-review", "path": "/work/skills/pr-review"},
        {"type": "text", "text": "PR #42"},
    ]


@pytest.mark.asyncio
async def test_a_skill_sent_during_a_live_turn_steers_into_it(harness):
    """Same rule as a prompt: Codex's own mechanism for adding to live work is
    `turn/steer`, so a skill invoked mid-turn joins that turn instead of racing
    a second one."""
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_SKILLS_LIST,
        result=skills_result(skill("pr-review", "/work/skills/pr-review")),
    )
    await driver.server_info()
    await harness.run_turn("start something")

    await driver.send_skill("pr-review")
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_STEER)["input"] == [
        {"type": "skill", "name": "pr-review", "path": "/work/skills/pr-review"}
    ]
    assert len(harness.frames(schema.METHOD_TURN_START)) == 1


@pytest.mark.asyncio
async def test_an_unpublished_skill_is_refused_rather_than_guessed(harness):
    """A name the driver never published has no path, and a turn started on a
    guessed path would run whatever happened to be there."""
    driver = await harness.boot()
    harness.server.rule(schema.METHOD_SKILLS_LIST, result=skills_result())
    await driver.server_info()

    with pytest.raises(KeyError):
        await driver.send_skill("never-published")
    assert harness.frames(schema.METHOD_TURN_START) == []


@pytest.mark.asyncio
async def test_the_declared_capabilities_match_what_the_driver_can_do(harness):
    """A capability set is what the UI renders from, so it may not claim a
    surface the driver lacks — the pairing that had already drifted once on
    `questions` (server/codex/capabilities.py)."""
    driver = await harness.boot()

    assert CODEX_CAPABILITIES.vendor_commands and CODEX_CAPABILITIES.vendor_skills
    assert driver.capabilities is CODEX_CAPABILITIES
    assert callable(driver.send_skill)
