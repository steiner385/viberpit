"""compute_repo_deploy unit tests (SPEC §3 — deploy status, awaiting buckets,
supersession, chain, inFlight).

The function reads only ``.number``, ``.merged_at``, ``.qa_live_at``,
``.prod_live_at``, ``.merge_commit_sha`` off each merge row, so a lightweight
SimpleNamespace stands in for a MergedPr ORM row — no DB needed (pure function).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from server.db import db_session, init_db
from server.db.models import MergedPr, RepoDeployConfig
from server.github.pr_status import PipelinePr, PullRequestInfo
from server.pipeline.deploy_status import compute_repo_deploy
from server.pipeline.timeline import upsert_merged_pr


_UNSET = object()


def _m(number, merged_at, *, qa=None, prod=None, sha=_UNSET):
    return SimpleNamespace(
        number=number, merged_at=merged_at, qa_live_at=qa, prod_live_at=prod,
        merge_commit_sha=(f"sha{number}" if sha is _UNSET else sha),
    )


_ENVS = [{"name": "qa", "liveSha": "abc", "reachable": True},
         {"name": "prod", "liveSha": None, "reachable": False}]


def test_empty_merges():
    d = compute_repo_deploy([], _ENVS)
    assert d == {
        "envs": _ENVS, "awaitingQa": 0, "awaitingProd": 0,
        "chain": {"entries": [], "inFlight": None, "supersededCount": 0},
    }


def test_stage_merged_only_awaits_qa():
    d = compute_repo_deploy([_m(10, "2026-07-12T00:00:00Z")], _ENVS)
    assert d["awaitingQa"] == 1 and d["awaitingProd"] == 0
    e = d["chain"]["entries"][0]
    assert e["stage"] == "merged" and e["superseded"] is False
    assert e["prNumber"] == 10 and e["sha"] == "sha10"
    assert d["chain"]["inFlight"] == {"prNumber": 10, "sha": "sha10", "stage": "merged"}


def test_stage_qa_awaits_prod():
    d = compute_repo_deploy([_m(10, "2026-07-12T00:00:00Z", qa="2026-07-12T01:00:00Z")], _ENVS)
    assert d["awaitingQa"] == 0 and d["awaitingProd"] == 1
    assert d["chain"]["entries"][0]["stage"] == "qa"
    assert d["chain"]["inFlight"]["stage"] == "qa"


def test_stage_prod_is_deployed_not_awaiting():
    d = compute_repo_deploy(
        [_m(10, "2026-07-12T00:00:00Z", qa="2026-07-12T01:00:00Z",
            prod="2026-07-12T02:00:00Z")], _ENVS)
    assert d["awaitingQa"] == 0 and d["awaitingProd"] == 0
    assert d["chain"]["entries"][0]["stage"] == "prod"
    # inFlight only picks non-prod entries → nothing in flight here.
    assert d["chain"]["inFlight"] is None
    assert d["chain"]["supersededCount"] == 0


def test_supersession_excludes_older_qa_from_awaiting_and_marks_chain():
    # A merges first (only QA), B merges later and reaches prod → A superseded.
    a = _m(1, "2026-07-12T00:00:00Z", qa="2026-07-12T00:30:00Z")
    b = _m(2, "2026-07-12T01:00:00Z", qa="2026-07-12T01:30:00Z", prod="2026-07-12T02:00:00Z")
    d = compute_repo_deploy([a, b], _ENVS)
    # A is neither awaitingQa nor awaitingProd (rolled into B's prod deploy).
    assert d["awaitingQa"] == 0 and d["awaitingProd"] == 0
    entries = {e["prNumber"]: e for e in d["chain"]["entries"]}
    assert entries[1]["superseded"] is True
    assert entries[2]["superseded"] is False
    assert d["chain"]["supersededCount"] == 1
    # inFlight skips the superseded A and B (prod) → nothing in flight.
    assert d["chain"]["inFlight"] is None


def test_supersession_input_order_independent():
    a = _m(1, "2026-07-12T00:00:00Z", qa="2026-07-12T00:30:00Z")
    b = _m(2, "2026-07-12T01:00:00Z", prod="2026-07-12T02:00:00Z")
    # Feed reversed / unordered; result identical (function sorts internally).
    d = compute_repo_deploy([a, b], _ENVS)
    d2 = compute_repo_deploy([b, a], _ENVS)
    assert d == d2
    assert d["awaitingQa"] == 0 and d["awaitingProd"] == 0
    # newest-first ordering: B (later merge) leads the chain.
    assert [e["prNumber"] for e in d["chain"]["entries"]] == [2, 1]


def test_inflight_picks_newest_non_prod_non_superseded():
    # Newest merge C is only merged (in flight); B reached prod; A superseded.
    a = _m(1, "2026-07-12T00:00:00Z", qa="2026-07-12T00:30:00Z")
    b = _m(2, "2026-07-12T01:00:00Z", prod="2026-07-12T02:00:00Z")
    c = _m(3, "2026-07-12T03:00:00Z")
    d = compute_repo_deploy([a, b, c], _ENVS)
    # C merged after B's prod → not superseded, not prod → in flight.
    assert d["chain"]["inFlight"] == {"prNumber": 3, "sha": "sha3", "stage": "merged"}
    assert d["awaitingQa"] == 1  # only C awaits QA
    assert d["awaitingProd"] == 0


def test_chain_limit_caps_entries_but_not_awaiting_counts():
    merges = [_m(i, f"2026-07-{10 + i:02d}T00:00:00Z") for i in range(1, 11)]  # 10 merged
    d = compute_repo_deploy(merges, _ENVS, chain_limit=8)
    assert len(d["chain"]["entries"]) == 8
    # awaiting counters iterate ALL merges, not just the chain window.
    assert d["awaitingQa"] == 10
    # Newest-first: PR 10 leads, PR 3 is the 8th (oldest kept).
    assert d["chain"]["entries"][0]["prNumber"] == 10
    assert d["chain"]["entries"][-1]["prNumber"] == 3


def test_null_sha_preserved_in_entry_and_inflight():
    d = compute_repo_deploy([_m(10, "2026-07-12T00:00:00Z", sha=None)], _ENVS)
    # SimpleNamespace default sha None → entry + inFlight carry sha None.
    e = d["chain"]["entries"][0]
    assert e["sha"] is None
    assert d["chain"]["inFlight"] == {"prNumber": 10, "sha": None, "stage": "merged"}


# --------------------------------------------------------------------------- #
# _build_state glue: real store rows → frame carries deploy + timeline          #
# (follows test_group_history.py::test_sim_ctx_reads_real_store_rows)           #
# --------------------------------------------------------------------------- #

# Relative anchor: always within the 7-day tracked_merged retention window.
_RECENT_MERGE = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)


@pytest.fixture()
def _clean_deploy_tables():
    async def wipe():
        await init_db()
        async with db_session() as db:
            await db.execute(delete(RepoDeployConfig))
            await db.execute(delete(MergedPr))
    asyncio.run(wipe())


class _FakeOrch:
    """Just the two async reads _deploy_ctx calls, backed by the live store."""

    def __init__(self, env_cache: dict):
        self._env_cache = env_cache

    async def deploy_config_list(self):
        # Same shape the orchestrator returns, read from the seeded rows.
        import json as _json
        from sqlalchemy import select
        async with db_session() as db:
            rows = (await db.execute(select(RepoDeployConfig))).scalars().all()
        return {
            r.repo: {"environments": _json.loads(r.environments or "[]"),
                     "enabled": bool(r.enabled)}
            for r in rows
        }

    async def deploy_status(self):
        return self._env_cache


def _merged_pr(number: int) -> PipelinePr:
    return PipelinePr(pr=PullRequestInfo(
        number=number, url=f"https://github.com/o/r/pull/{number}", state="merged",
        checks="passing", title="t", stage="", repo="o/r", merged=True,
    ))


def test_build_state_glue_frame_carries_deploy_and_timeline(_clean_deploy_tables):
    from server.api.pipeline import _deploy_ctx
    from server.pipeline.dashboard_state import dashboard_state

    async def run():
        # Seed a configured repo + a merged-PR row with qa live (awaiting prod).
        async with db_session() as db:
            db.add(RepoDeployConfig(
                repo="o/r",
                environments='[{"name":"qa","healthUrl":"http://x/health"}]',
                enabled=True,
            ))
        await upsert_merged_pr(
            "o/r", 7, merged_at=_RECENT_MERGE,
            merge_commit_sha="deadbeef", qa_live_at="2026-07-12T01:00:00Z",
        )
        env_cache = {"o/r": [{"name": "qa", "liveSha": "deadbeef", "reachable": True}]}
        orch = _FakeOrch(env_cache)
        prs_by_repo = {"o/r": [_merged_pr(7)]}

        deploy_ctx, timeline_ctx = await _deploy_ctx(orch, prs_by_repo)
        # Deploy block computed from the real store row + env cache.
        assert deploy_ctx["o/r"]["awaitingProd"] == 1
        assert deploy_ctx["o/r"]["awaitingQa"] == 0
        assert deploy_ctx["o/r"]["envs"] == env_cache["o/r"]
        # Timeline map carries the merged PR present in the frame.
        assert timeline_ctx["o/r"][7]["qaLiveAt"] == "2026-07-12T01:00:00Z"

        # End-to-end: the assembled frame surfaces deploy + hasDeploy + timeline.
        st = dashboard_state(
            prs_by_repo, {}, {}, {"o/r": False}, generated_at="x",
            deploy_ctx=deploy_ctx, timeline_ctx=timeline_ctx,
        )
        repo = st["repos"][0]
        assert repo["hasDeploy"] is True
        assert repo["deploy"]["awaitingProd"] == 1
        pr7 = repo["prs"][0]
        assert pr7["timeline"]["qaLiveAt"] == "2026-07-12T01:00:00Z"
        assert pr7["stage"]["stage"] == "awaiting-prod"

    asyncio.run(run())


def test_deploy_ctx_skips_disabled_and_unconfigured(_clean_deploy_tables):
    from server.api.pipeline import _deploy_ctx

    async def run():
        async with db_session() as db:
            db.add(RepoDeployConfig(repo="o/off", environments="[]", enabled=False))
        orch = _FakeOrch({})
        deploy_ctx, timeline_ctx = await _deploy_ctx(orch, {"o/off": [], "o/none": []})
        # Disabled config → no deploy block; unconfigured repo → absent.
        assert deploy_ctx == {} and timeline_ctx == {}

    asyncio.run(run())
