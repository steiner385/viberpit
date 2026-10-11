"""Regression: a feature-map map-diff must not re-fire for a capability whose doc
already merged, nor run twice while one PR is in flight (#2926 #2927 #2929
#2931 #2932).

On 2026-09-08 00:52–01:00Z five KinDash capabilities (ci-test-reliability,
ui-framework-overhaul, forced-deploy-audit, web-accessibility-remediation,
accessibility-verification-coverage) were each re-dispatched 1.5–3.5 h after
the map-diff PR for that capability merged. Every merged doc carried
``capability:``/``status:`` frontmatter (no ``slug:``). The parser at the time
rejected that, so pm-sync indexed no ``Feature``. Half (a) of the drift latch (a
``Feature`` with the suggestion's slug) never held. Half (b) (a non-terminal
session) had already released, and the next tick dispatched again.

The first group below runs the merged docs through the real index path
(parse, plan, apply) into the latch and the engine. Two later parser changes
each rescue these docs: the ``capability:`` key (#1462) and the slug taken from
the ``<slug>.md`` filename (#2092). Revert ``parse_feature_doc`` to read only
``slug:`` and all five go red.

The second group covers the hole that remained. Once a map-diff session marks
complete with its PR armed, completion archives it while the PR is still open
(``met_bar``: armed counts as met). Nothing is indexed yet and nothing is on
the default branch, so neither (a) nor (b) holds and the next tick opened a
second session and PR for the same capability.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.automations.context import AutomationContext
from server.automations.pm_triage import PmTriageLoop
from server.db import db_session, init_db
from server.db.models import (
    Feature, Project, ProjectRepo, Session as DbSession, TriageProposal, WorkItem)
from server.pm.engine import (
    Idle, NoFitCluster, ProposeFeatureMapDiff, TriageState, next_triage_action)
from server.tests.pm_ownership_fixtures import identity_repositories

REPO = "cairnea/KinDash"
SLUG = "kindash"


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories",
                        identity_repositories)


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            yield url


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, level, summary, **kw):
        self.events.append((level, summary, kw))


async def _seed(db_url: str) -> str:
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug=SLUG, name="KinDash", onboarding_state="map_merged",
                    automation_policy={"trusted_authors": ["tony"]})
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True,
                           backfill_done=True))
        return p.id


async def _add_cluster(suggestion: str, numbers: list[int]) -> None:
    async with db_session() as db:
        for n in numbers:
            w = WorkItem(author="tony", repo=REPO, number=n, kind="issue",
                         title=f"i{n}", state="open", triage_state="classified")
            db.add(w)
            await db.flush()
            db.add(TriageProposal(work_item_id=w.id, status="accepted",
                                  no_fit_suggestion=suggestion))


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._pm_onboard_ctx = AutomationContext("pm-onboard")
    orch._pm_triage_ctx = _Emitter()
    return orch


def _merged_doc(capability: str, title: str) -> str:
    """The frontmatter dialect every one of the five merged docs used (KinDash
    PRs #15677, #15679, #15688, #15689 and #15690)."""
    return (f"---\ncapability: {capability}\nstatus: proposed\n"
            "area: engineering\nowner: engineering\nadded: 2026-09-07\n---\n\n"
            f"# {title}\n\n## Problem\n\nDriving items are cited below.\n")


# (issue, capability, title, driving items named in the report)
INCIDENT = [
    ("2926", "ci-test-reliability", "CI test reliability",
     [15601, 15602, 15603, 15604, 15605]),
    ("2927", "ui-framework-overhaul", "UI framework overhaul",
     [15138, 15139, 15254, 15256, 15257, 15258, 15259, 15260, 15263, 15264,
      15265, 15266, 15268, 15568]),
    ("2929", "forced-deploy-audit", "Forced deploy audit",
     [14915, 14964, 15003, 15064, 15084, 15115, 15134, 15211, 15333, 15380, 15419]),
    ("2931", "web-accessibility-remediation", "Web accessibility remediation",
     [14881, 15482, 15486, 15499, 15500, 15501, 15502, 15503]),
    ("2932", "accessibility-verification-coverage",
     "Accessibility verification coverage",
     [15505, 15506, 15507, 15508, 15509, 15510]),
]


async def _index_default_branch(project_id: str, docs: list[tuple[str, str]]) -> None:
    """What pm-sync does after a default-branch read: parse, plan, apply."""
    from server.pm.feature_index import merge_branch_over_main, plan_feature_index
    from server.pm.feature_sync_seams import apply_feature_plan, list_feature_rows
    parsed, proposed, skipped = merge_branch_over_main(
        docs, [], pr_number=None, pr_url=None)
    assert skipped == []
    plan = plan_feature_index(await list_feature_rows(project_id), parsed,
                              read_sha="f5cd7edcb", proposed=proposed)
    await apply_feature_plan(project_id, plan)


# --------------------------------------------------------------------------- #
# The incident: a merged doc must latch its cluster                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("issue,capability,title,numbers", INCIDENT,
                         ids=[f"issue_{i[0]}_{i[1]}" for i in INCIDENT])
async def test_issue_2926_2927_2929_2931_2932_merged_capability_doc_latches_its_cluster(
        db_env, issue, capability, title, numbers):
    pid = await _seed(db_env)
    await _add_cluster(capability, numbers)
    orch = _orch()
    state = await orch._pm_triage_count_state()
    # Negative control: before the merge, the cluster drives the drift branch.
    before = next_triage_action(replace(state, mode="propose"), {"drift_threshold": 5})
    assert isinstance(before, ProposeFeatureMapDiff) and before.suggestion == capability

    await _index_default_branch(
        pid, [(f"docs/product/features/{capability}.md", _merged_doc(capability, title))])

    async with db_session() as db:
        feat = (await db.execute(select(Feature).where(
            Feature.project_id == pid, Feature.slug == capability))).scalar_one()
    # The merged doc says ``status: proposed``. It is still merged
    # (commit_sha is set), so a latch that ignored "proposed" Features would
    # re-open this exact duplicate for three of the five capabilities.
    assert feat.state == "proposed" and feat.commit_sha == "f5cd7edcb"

    state = await orch._pm_triage_count_state()
    assert capability in state.open_map_diff_suggestions
    after = next_triage_action(replace(state, mode="propose"), {"drift_threshold": 5})
    assert isinstance(after, Idle), after


# --------------------------------------------------------------------------- #
# The remaining hole: PR still in flight after completion archived the session #
# --------------------------------------------------------------------------- #


def _archived_map_diff(capability: str, *, prs: list[dict] | None,
                       archived_at: datetime | None = None,
                       archived_by: str = "completion") -> DbSession:
    """A map-diff row as ``_on_complete_auto_archive`` leaves it. The archive
    context carries ``PrFacts.to_prompt()`` as the PR snapshot."""
    context = None if prs is None else {
        "repo": REPO, "goal": "propose a capability", "goal_source": "completion",
        "pr_state": {"has_repo": True, "prs": prs},
        "reason": "pm-onboard: completion marker — goal met"}
    return DbSession(
        name=f"pm map-diff {SLUG}", account_name="a1", cwd="/tmp/x",
        origin="pm-onboard", origin_ref=f"{SLUG}:map-diff:{capability}", repo=REPO,
        status="archived", archived_by=archived_by,
        archived_at=archived_at or _utcnow(), archive_context=context)


def _utcnow() -> datetime:
    """Naive UTC, the convention of every persisted timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pr(state: str, *, number: int = 15688, armed: bool = True) -> dict:
    return {"number": number, "url": f"https://github.com/{REPO}/pull/{number}",
            "state": state, "is_draft": state == "draft",
            "auto_merge_enabled": armed, "in_merge_queue": False,
            "checks": "pending", "is_docs_only": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_state", ["open", "draft"])
async def test_map_diff_pr_in_flight_after_completion_archive_latches_its_cluster(
        db_env, pr_state):
    """#2927 shape: the session opens its PR, the repo's bot arms it, and the
    session marks complete. ``met_bar`` says armed is met, so completion
    archives the session while the PR is still open. The latch has to hold
    until the PR merges and pm-sync indexes the doc."""
    await _seed(db_env)
    await _add_cluster("ui-framework-overhaul", [15138, 15139, 15254, 15256, 15257])
    async with db_session() as db:
        db.add(_archived_map_diff("ui-framework-overhaul", prs=[_pr(pr_state)]))
    state = await _orch()._pm_triage_count_state()
    assert state.open_map_diff_suggestions == ("ui-framework-overhaul",)
    action = next_triage_action(replace(state, mode="propose"), {"drift_threshold": 5})
    assert isinstance(action, Idle), action


@pytest.mark.asyncio
async def test_in_flight_latch_survives_a_restart(db_env):
    """It is read from the database, not from process memory, so a fresh
    orchestrator sees it too."""
    await _seed(db_env)
    await _add_cluster("forced-deploy-audit", [14915, 14964, 15003, 15064, 15084])
    async with db_session() as db:
        db.add(_archived_map_diff("forced-deploy-audit", prs=[_pr("open")]))
    assert (await _orch()._pm_triage_count_state()).open_map_diff_suggestions == (
        "forced-deploy-audit",)
    assert (await _orch()._pm_triage_count_state()).open_map_diff_suggestions == (
        "forced-deploy-audit",)


@pytest.mark.asyncio
@pytest.mark.parametrize("prs", [
    None,                       # no snapshot at all (pre-existing behaviour)
    [],                         # archived with no PR
    [_pr("merged")],            # merged: half (a) owns this once pm-sync indexes it
    [_pr("closed", armed=False)],  # a human closed it: not in flight
], ids=["no-context", "no-pr", "merged", "closed"])
async def test_archived_map_diff_without_an_open_pr_does_not_latch(db_env, prs):
    await _seed(db_env)
    await _add_cluster("ci-test-reliability", [1, 2, 3, 4, 5])
    async with db_session() as db:
        db.add(_archived_map_diff("ci-test-reliability", prs=prs))
    state = await _orch()._pm_triage_count_state()
    assert state.open_map_diff_suggestions == ()


@pytest.mark.asyncio
async def test_stale_open_pr_snapshot_stops_latching(db_env):
    """The snapshot is frozen at archive time. A PR that merged would have
    latched through (a) long before, so an "open" snapshot this old is a PR
    nobody merged. It must not hold back new work on the capability for good."""
    from server.sessions.orchestrator import MAP_DIFF_IN_FLIGHT_MAX_AGE_S
    await _seed(db_env)
    await _add_cluster("ci-test-reliability", [1, 2, 3, 4, 5])
    old = _utcnow() - timedelta(seconds=MAP_DIFF_IN_FLIGHT_MAX_AGE_S, hours=1)
    async with db_session() as db:
        db.add(_archived_map_diff("ci-test-reliability", prs=[_pr("open")],
                                  archived_at=old))
    state = await _orch()._pm_triage_count_state()
    assert state.open_map_diff_suggestions == ()


@pytest.mark.asyncio
async def test_in_flight_latch_is_scoped_to_the_owning_project(db_env):
    """The key is ``<project>:map-diff:<suggestion>``. Another product's
    in-flight PR for a same-named capability must not latch this one."""
    await _seed(db_env)
    await _add_cluster("ci-test-reliability", [1, 2, 3, 4, 5])
    row = _archived_map_diff("ci-test-reliability", prs=[_pr("open")])
    row.origin_ref = "viberpit:map-diff:ci-test-reliability"
    async with db_session() as db:
        db.add(row)
    state = await _orch()._pm_triage_count_state()
    assert state.open_map_diff_suggestions == ()


# --------------------------------------------------------------------------- #
# The feed must not claim a dispatch that did not happen                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skipped_map_diff_dispatch_is_not_reported_as_proposed():
    """In the incident the feed said "proposed feature-map diff" even when the
    dispatch seam spawned nothing. That is exactly the claim an operator reads
    as "it re-fired"."""
    ctx = _Emitter()
    cluster = NoFitCluster(suggestion="ci-test-reliability",
                           item_numbers=(1, 2, 3, 4, 5), repo=REPO)
    state = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                        no_fit_clusters=(cluster,), stale_features=(),
                        day_spawns=0, day_cap=8)
    calls: list[str] = []

    async def none(*_a, **_k):
        return None

    async def empty(*_a, **_k):
        return []

    async def count_state():
        return state

    async def propose(suggestion, _numbers, _repo):
        calls.append(suggestion)
        return None  # skipped: already covered / no owner / no account

    async def read_config():
        return "propose", {"drift_threshold": 5, "batch_size": 50,
                           "max_spawns_per_day": 8}

    loop = PmTriageLoop(ctx, read_config=read_config, list_pending=empty,
                        auto_link=none, count_state=count_state, classify=none,
                        propose_map_diff=propose, mark_stale=none,
                        clock=lambda: 1000.0)
    await loop.tick()
    assert calls == ["ci-test-reliability"]
    assert [kw.get("kind") for _l, _s, kw in ctx.events if
            kw.get("kind") == "drift_proposed"] == []


@pytest.mark.asyncio
async def test_dispatch_skip_for_an_already_merged_doc_is_reported(db_env, monkeypatch):
    """When the dispatch seam finds the capability already covered on the default
    branch, it records why it spawned nothing."""
    await _seed(db_env)
    orch = _orch()
    spawned: list[str] = []

    async def covered(_rec, **_kw):
        return True

    async def spawn(*a, **kw):
        spawned.append(kw.get("suggestion"))

    monkeypatch.setattr(orch, "_pm_onboard_observe_map_diff_not_needed", covered)
    monkeypatch.setattr(orch, "pm_onboard_spawn_map", spawn)
    assert await orch._pm_triage_propose_map_diff(
        "ci-test-reliability", (1, 2, 3, 4, 5), REPO) is None
    assert spawned == []
    kinds = [(kw.get("kind"), kw.get("target")) for _l, _s, kw in
             orch._pm_triage_ctx.events]
    assert ("skipped", f"{SLUG}:map-diff:ci-test-reliability") in kinds


# --------------------------------------------------------------------------- #
# The live PR-population cache outranks the frozen archive snapshot            #
# --------------------------------------------------------------------------- #


class _Population:
    """The ``PrPopulationPoller.cached(repo)`` surface the latch reads."""

    def __init__(self, prs: dict[str, list]) -> None:
        self._prs = prs

    def cached(self, repo):
        return tuple(self._prs.get(repo, ()))


def _cached_pr(state: str, number: int = 15688):
    from server.github.pr_status import PullRequestInfo
    return PullRequestInfo(number=number, state=state, checks="pending",
                           title="docs(product): propose a capability",
                           url=f"https://github.com/{REPO}/pull/{number}",
                           is_draft=state == "draft", repo=REPO)


async def _latched_with(db_env, *, cache: list | None, days_ago: float) -> tuple:
    await _seed(db_env)
    await _add_cluster("ui-framework-overhaul", [15138, 15139, 15254, 15256, 15257])
    async with db_session() as db:
        db.add(_archived_map_diff("ui-framework-overhaul", prs=[_pr("open")],
                                  archived_at=_utcnow() - timedelta(days=days_ago)))
    orch = _orch()
    if cache is not None:
        orch._pr_population = _Population({REPO: cache})
    return (await orch._pm_triage_count_state()).open_map_diff_suggestions


@pytest.mark.asyncio
async def test_unreviewed_map_diff_pr_still_open_on_day_8_keeps_the_latch(db_env):
    """Map-diff PRs never arm auto-merge, so a review can take longer than the
    snapshot's 7-day trust window. While the cache still sees the PR open, the
    capability must not get a second PR."""
    assert await _latched_with(db_env, cache=[_cached_pr("open")], days_ago=8) == (
        "ui-framework-overhaul",)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed", "merged"])
async def test_pr_closed_or_merged_in_cache_releases_the_latch(db_env, state):
    """The snapshot froze "open", but the cache knows better. A merged PR latches
    through the indexed Feature once pm-sync sees it. A closed one is a human's
    no, and new work on the capability may be proposed again."""
    assert await _latched_with(db_env, cache=[_cached_pr(state)], days_ago=1) == ()


@pytest.mark.asyncio
async def test_pr_missing_from_cache_falls_back_to_the_snapshot_window(db_env):
    """No cache entry for the PR (poller not warmed up, repo not watched):
    the 7-day snapshot rule decides."""
    assert await _latched_with(db_env, cache=[_cached_pr("open", number=1)],
                               days_ago=1) == ("ui-framework-overhaul",)


@pytest.mark.asyncio
async def test_pr_missing_from_cache_past_the_window_releases(db_env):
    assert await _latched_with(db_env, cache=[], days_ago=8) == ()


# --------------------------------------------------------------------------- #
# One spelling of the map-diff dedupe key                                      #
# --------------------------------------------------------------------------- #


def test_map_diff_key_round_trips():
    from server.pm.map_resolution import map_diff_key, parse_map_diff_key
    key = map_diff_key("kindash", "ci-test-reliability")
    assert key == "kindash:map-diff:ci-test-reliability"
    assert parse_map_diff_key(key) == ("kindash", "ci-test-reliability")


@pytest.mark.parametrize("ref", [None, "", "kindash:feature-map", "kindash:map-diff:",
                                 "kindash:map-diff:   ", ":map-diff:x"])
def test_parse_map_diff_key_rejects_non_map_diff_refs(ref):
    from server.pm.map_resolution import parse_map_diff_key
    assert parse_map_diff_key(ref) is None
