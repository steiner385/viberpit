"""PM onboarding / classification ⇄ Codex acceptance suite (#1268, #1269).

The two PM session origins — ``pm-onboard`` (feature-map synthesis + its
completeness critic) and ``pm-classify`` (backlog triage proposals) — were the
last session-spawning origins with no engine declaration at all: they are not
registry automations, so ``AutomationContext.launch_plan`` fell through its
``KeyError`` branch to ``claude_only`` and no ``engine`` config field existed for
an operator to opt in with. This suite is the gate on widening them.

It covers everything decidable without an OpenAI account:

* the three prompt contracts, which are engine-neutral apart from the commit
  attribution trailer (the critic prompt had NO trailer at all — it commits and
  pushes, so a Codex critic was authoring unattributed commits);
* the spawn paths, which resolve the engine ONCE and thread it into the prompt,
  the account gate and the session — the defect #1323 found in three of the four
  SDLC/backlog spawn sites, and which all three PM sites had;
* that a refused launch costs nothing and advances nothing (no account probe, no
  clone, no ``onboarding_state`` transition, no ingest chain);
* the harvest, which reads normalized ``TextDelta`` events and so is already
  engine-neutral — Codex reasoning summaries arrive as ``ThinkingDelta`` and must
  not corrupt the fenced JSON a classify session's proposals are parsed from;
* the three gates that stay shut after the widening.

The live half — a real Codex session classifying a real bounded batch — is
``server/tests/test_pm_codex_live.py``, opt-in so CI never spends
OpenAI quota.
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ccswitch.engines import AgentEngine
from server.automations.engine_policy import (
    CODEX_NETWORK_OPT_IN_MODE,
    ENGINE_CONFIG_KEY,
    EnginePolicy,
    audited_policy_map,
    default_engine,
    escalate_for_codex_network_opt_in,
    resolve_engine,
    unattended_launch_blocker,
)
from server.db import db_session, init_db
from server.db.models import Feature, Project, ProjectRepo, WorkItem
from server.pm.classification import (
    build_classification_prompt,
    build_coverage_critic_prompt,
    build_map_synthesis_prompt,
    parse_classification_output,
    FeatureBrief,
    WorkItemBrief,
)
from server.sessions.completion import commit_trailer
from server.sessions.events import TextDelta, ThinkingDelta

CLAUDE = AgentEngine.CLAUDE_CODE
CODEX = AgentEngine.CODEX


# --------------------------------------------------------------------------- #
# 1. Prompt contracts — one template per job, two attributions.                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("scope,kwargs", [
    ("initial", {}),
    ("diff", {"suggestion": "billing", "item_numbers": (1, 2)}),
])
def test_the_map_synthesis_prompt_attributes_commits_to_the_running_engine(scope, kwargs):
    """A Codex map session must not be told to co-author its commits as Claude."""
    prompt = build_map_synthesis_prompt(
        slug="p", repo="o/r", default_branch="main", open_count=1, closed_count=0,
        scope=scope, engine=CODEX, **kwargs)
    assert commit_trailer(CODEX) in prompt
    assert commit_trailer(CLAUDE) not in prompt


@pytest.mark.parametrize("scope,kwargs", [
    ("initial", {}),
    ("diff", {"suggestion": "billing", "item_numbers": (1, 2)}),
])
def test_the_map_synthesis_prompt_is_identical_apart_from_the_trailer(scope, kwargs):
    """One semantic template, two attributions — not two prompt builders."""
    def built(engine):
        return build_map_synthesis_prompt(
            slug="p", repo="o/r", default_branch="main", open_count=1,
            closed_count=0, scope=scope, engine=engine, **kwargs
        ).replace(commit_trailer(engine), "TRAILER")

    assert built(CLAUDE) == built(CODEX)


def test_the_critic_prompt_attributes_its_commits_to_the_running_engine():
    """The critic COMMITS AND PUSHES, so it needs the same attribution contract
    the synthesis prompt has. It shipped with no trailer line at all — every
    critic commit, on either engine, was unattributed."""
    prompt = build_coverage_critic_prompt(
        slug="p", repo="o/r", default_branch="main", branch="pm/feature-map-p",
        open_count=0, closed_count=0, engine=CODEX)
    assert commit_trailer(CODEX) in prompt
    assert commit_trailer(CLAUDE) not in prompt


def test_the_critic_prompt_is_identical_apart_from_the_trailer():
    def built(engine):
        return build_coverage_critic_prompt(
            slug="p", repo="o/r", default_branch="main", branch="b",
            open_count=0, closed_count=0, engine=engine
        ).replace(commit_trailer(engine), "TRAILER")

    assert built(CLAUDE) == built(CODEX)


def test_the_critic_prompt_keeps_its_propose_only_gates_on_either_engine():
    """Augment the SAME PR, never open a second, never arm auto-merge — the human
    curation gate is the whole point of the onboarding chain."""
    for engine in (CLAUDE, CODEX):
        prompt = build_coverage_critic_prompt(
            slug="p", repo="o/r", default_branch="main", branch="pm/feature-map-p",
            open_count=0, closed_count=0, engine=engine)
        assert "DO NOT open a new pull request" in prompt
        assert "DO NOT arm auto-merge" in prompt


def test_the_map_synthesis_prompt_keeps_its_propose_only_gates_on_either_engine():
    for engine in (CLAUDE, CODEX):
        prompt = build_map_synthesis_prompt(
            slug="p", repo="o/r", default_branch="main", open_count=1,
            closed_count=0, engine=engine)
        assert "non-draft PR" in prompt
        assert "Do NOT arm auto-merge" in prompt


def test_the_classification_prompt_needs_no_engine_at_all():
    """A classify session commits nothing — its deliverable is a fenced JSON
    array in its final message — so the one engine-specific line in the other two
    prompts has no place here. Asserted so nobody "fixes" the asymmetry by
    threading an unused engine through it."""
    prompt = build_classification_prompt(
        items=[WorkItemBrief(number=1, kind="issue", title="t", body="b",
                             labels=(), url="")],
        features=[FeatureBrief(slug="f", title="F", summary="s")])
    assert commit_trailer(CLAUDE) not in prompt
    assert commit_trailer(CODEX) not in prompt
    lowered = prompt.lower()
    for claude_ism in ("claude", "anthropic", "subagent", "skill"):
        assert claude_ism not in lowered, f"classify prompt leaks {claude_ism!r}"


# --------------------------------------------------------------------------- #
# 2. The audited ceiling and the gates that stay shut after widening it.       #
# --------------------------------------------------------------------------- #

def test_both_pm_origins_declare_a_codex_allowed_ceiling():
    audited = audited_policy_map()
    assert audited["pm-onboard"] is EnginePolicy.CODEX_ALLOWED
    assert audited["pm-classify"] is EnginePolicy.CODEX_ALLOWED


def test_an_opted_in_pm_origin_still_defaults_to_claude():
    """``codex_allowed`` grants the option, not the traffic."""
    assert default_engine(EnginePolicy.CODEX_ALLOWED) is CLAUDE
    assert resolve_engine(EnginePolicy.CODEX_ALLOWED, {},
                          automations_enabled=True) is CLAUDE


def test_the_kill_switch_clamps_an_opted_in_pm_origin_back_to_claude():
    config = {ENGINE_CONFIG_KEY: "codex"}
    assert resolve_engine(EnginePolicy.CODEX_ALLOWED, config,
                          automations_enabled=False) is CLAUDE
    assert resolve_engine(EnginePolicy.CODEX_ALLOWED, config,
                          automations_enabled=True) is CODEX


def test_an_unattended_codex_pm_session_is_refused_without_the_network_opt_in():
    """A map or critic session pushes a branch and opens a PR; a classify session
    is read-only but still clones. ``full-auto`` is network-isolated on Codex, so
    the launch is refused up front rather than discovered at ``git push``."""
    assert unattended_launch_blocker(CODEX, "full-auto") is not None
    assert unattended_launch_blocker(CLAUDE, "full-auto") is None
    mode, note = escalate_for_codex_network_opt_in(CODEX, "full-auto",
                                                   network_opt_in=True)
    assert mode == CODEX_NETWORK_OPT_IN_MODE and note
    assert unattended_launch_blocker(CODEX, mode) is None


# --------------------------------------------------------------------------- #
# 3. The spawn paths — one engine decision, threaded everywhere.               #
# --------------------------------------------------------------------------- #

class _Ctx:
    """Fake AutomationContext modelling the two calls a spawn site makes."""

    def __init__(self, engine=CLAUDE, *, refuse=False):
        self._engine = engine
        self._refuse = refuse
        self.plans = []
        self.calls = []

    async def launch_plan_or_refuse(self, origin_ref):
        self.plans.append(origin_ref)
        if self._refuse:
            return None
        return self._engine, {}

    async def spawn_session(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(id=f"sess-{len(self.calls)}", model=None)


def _bare_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator.__new__(SessionOrchestrator)


def _mk_async(value, *, record=None):
    async def _f(*a, **k):
        if record is not None:
            record.append((a, k))
        return value
    return _f


def _exploding(name):
    async def _f(*a, **k):
        raise AssertionError(f"{name} must not run for a refused launch")
    return _f


async def _seed(db_url: str) -> str:
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="proj", name="proj", onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True,
                           backfill_done=True))
        db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                       state="live", summary="s"))
        for n in range(2):
            db.add(WorkItem(repo="o/r", number=100 + n, kind="issue",
                            title=f"Bug {n}", state="open",
                            triage_state="unclassified"))
        return p.id


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


def _wire(orch, *, account_probe=None):
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("main")
    orch._automation_account_for = _mk_async("acct", record=account_probe)
    orch._spawn = lambda coro, name=None: coro.close()


@pytest.mark.asyncio
async def test_map_synthesis_spawns_on_the_engine_it_resolved(db_env):
    """One resolution, threaded into the account gate, the prompt and the
    session — not re-resolved by ``spawn_session`` off a second config read."""
    await _seed(db_env)
    orch = _bare_orch()
    ctx = _Ctx(CODEX)
    orch._pm_onboard_ctx = ctx
    probe: list = []
    _wire(orch, account_probe=probe)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        rec = await orch.pm_onboard_spawn_map("proj")

    assert rec is not None
    assert ctx.plans == ["proj:feature-map"]
    call = ctx.calls[0]
    assert call["engine"] is CODEX
    assert commit_trailer(CODEX) in call["initial_prompt"]
    assert commit_trailer(CLAUDE) not in call["initial_prompt"]
    assert call["detail"]["engine"] == "codex"
    assert probe[0][1]["engine"] is CODEX, "the account gate probed the wrong pool"


@pytest.mark.asyncio
async def test_a_refused_map_synthesis_costs_nothing_and_advances_nothing(db_env):
    """No account probe, no clone, and the project stays where it was: a refusal
    must not leave the wizard showing a proposal nobody is producing."""
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_onboard_ctx = _Ctx(refuse=True)
    orch._effective_repo_root = _exploding("_effective_repo_root")
    orch._sdlc_default_branch = _exploding("_sdlc_default_branch")
    orch._automation_account_for = _exploding("_automation_account_for")
    with patch("ccswitch.gitrepos.ensure_clone",
               side_effect=AssertionError("clone must not run for a refusal")):
        assert await orch.pm_onboard_spawn_map("proj") is None

    from sqlalchemy import select
    async with db_session() as db:
        proj = (await db.execute(select(Project).where(
            Project.slug == "proj"))).scalar_one()
        assert proj.onboarding_state == "map_merged"


@pytest.mark.asyncio
async def test_the_coverage_critic_spawns_on_the_engine_it_resolved(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    ctx = _Ctx(CODEX)
    orch._pm_onboard_ctx = ctx
    probe: list = []
    _wire(orch, account_probe=probe)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        rec = await orch._pm_onboard_spawn_critic("proj", "o/r", "pm/feature-map-proj")

    assert rec is not None
    assert ctx.plans == ["proj:feature-map-critic"]
    call = ctx.calls[0]
    assert call["engine"] is CODEX
    assert commit_trailer(CODEX) in call["initial_prompt"]
    assert probe[0][1]["engine"] is CODEX


@pytest.mark.asyncio
async def test_a_refused_critic_never_clones(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_onboard_ctx = _Ctx(refuse=True)
    orch._effective_repo_root = _exploding("_effective_repo_root")
    orch._automation_account_for = _exploding("_automation_account_for")
    with patch("ccswitch.gitrepos.ensure_clone",
               side_effect=AssertionError("clone must not run for a refusal")):
        assert await orch._pm_onboard_spawn_critic("proj", "o/r", "b") is None


@pytest.mark.asyncio
async def test_classification_spawns_on_the_engine_it_resolved(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    ctx = _Ctx(CODEX)
    orch._pm_classify_ctx = ctx
    probe: list = []
    _wire(orch, account_probe=probe)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        n = await orch.pm_classify_run("proj")

    assert n == 2
    call = ctx.calls[0]
    assert call["engine"] is CODEX
    assert call["detail"]["engine"] == "codex"
    assert probe[0][1]["engine"] is CODEX
    # The chunk dedupe key is unchanged by the engine — a Claude chunk and a
    # Codex chunk over the same items must still collide.
    assert ctx.plans == [call["origin_ref"]]
    assert call["origin_ref"].startswith("proj:classify:")


@pytest.mark.asyncio
async def test_a_refused_classification_dispatches_nothing_and_starts_no_ingest(db_env):
    """Returning 0 is the contract the caller releases its claim on; the harvest
    chain must not be started for a session that does not exist."""
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_classify_ctx = _Ctx(refuse=True)
    orch._effective_repo_root = _exploding("_effective_repo_root")
    orch._automation_account_for = _exploding("_automation_account_for")
    spawned: list = []
    orch._spawn = lambda coro, name=None: (spawned.append(name), coro.close())
    with patch("ccswitch.gitrepos.ensure_clone",
               side_effect=AssertionError("clone must not run for a refusal")):
        assert await orch.pm_classify_run("proj") == 0
    assert spawned == []

    from sqlalchemy import select
    async with db_session() as db:
        proj = (await db.execute(select(Project).where(
            Project.slug == "proj"))).scalar_one()
        assert proj.onboarding_state == "map_merged"


# --------------------------------------------------------------------------- #
# 4. Harvest — normalized events, so already engine-neutral. Pinned anyway.    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_classify_harvest_reads_normalized_text_and_ignores_reasoning():
    """``server/codex/translator.py`` renders an agent message as ``TextDelta``
    and a reasoning summary as ``ThinkingDelta``. The harvest takes only the
    former, so a Codex session that muses about JSON before answering still
    yields exactly its final fenced array."""
    fenced = ('```json\n[{"number": 100, "kind": "issue", '
              '"feature_slug": "sessions", "rationale": "r", '
              '"confidence_level": "high"}]\n```')
    window = SimpleNamespace(events=[
        ThinkingDelta("t1", "let me think about [{bogus json}] first"),
        TextDelta("t1", fenced[:20]),
        TextDelta("t1", fenced[20:]),
    ])
    orch = _bare_orch()
    orch.transcript_window = _mk_async(window)

    text = await orch._pm_classify_output_text("sess-1")
    assert text == fenced
    parsed = parse_classification_output(text)
    assert [p.number for p in parsed] == [100]
