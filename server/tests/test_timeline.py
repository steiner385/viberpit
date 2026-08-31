"""Merged-PR timeline store unit tests (SPEC §2 — PRTIMELINE CAPTURE)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from server.db import db_session
from server.db.models import MergedPr
from server.github.pr_population import PrPopulationPoller
from server.pipeline.timeline import (
    mark_env_live,
    timelines_for,
    tracked_merged,
    upsert_merged_pr,
)

import pytest


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Every test gets its own DB — table-wide asserts need no wiping (#728)."""


# --------------------------------------------------------------------------- #
# upsert_merged_pr — first-value-wins + writer-invariant merged_at
# --------------------------------------------------------------------------- #


def test_upsert_inserts_row_with_all_waypoints():
    async def run():
        await upsert_merged_pr(
            "o/r", 7,
            merged_at="2026-07-12T00:00:00Z",
            title="feat", url="u7", merge_commit_sha="abc",
            created_at="2026-07-11T00:00:00Z",
            first_green_at="2026-07-11T12:00:00Z",
            enqueued_at="2026-07-11T23:00:00Z",
        )
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got is not None
            assert got.merged_at == "2026-07-12T00:00:00Z"
            assert got.title == "feat"
            assert got.merge_commit_sha == "abc"
            assert got.created_at == "2026-07-11T00:00:00Z"
            assert got.first_green_at == "2026-07-11T12:00:00Z"
            assert got.enqueued_at == "2026-07-11T23:00:00Z"
            assert got.qa_live_at is None
            assert got.prod_live_at is None

    asyncio.run(run())


def test_upsert_first_value_wins_per_column():
    async def run():
        # First insert leaves first_green_at NULL.
        await upsert_merged_pr(
            "o/r", 7, merged_at="2026-07-12T00:00:00Z", title="orig",
            first_green_at=None,
        )
        # Second upsert supplies first_green_at (NULL → fills) but a different
        # title (already set → must be ignored).
        await upsert_merged_pr(
            "o/r", 7, merged_at="2026-07-12T09:00:00Z", title="changed",
            first_green_at="2026-07-11T12:00:00Z",
        )
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            # First title wins.
            assert got.title == "orig"
            # NULL column filled by the later write.
            assert got.first_green_at == "2026-07-11T12:00:00Z"

    asyncio.run(run())


def test_upsert_merged_at_is_writer_invariant_unchanged_on_conflict():
    async def run():
        await upsert_merged_pr("o/r", 7, merged_at="2026-07-12T00:00:00Z")
        # A later observation with a different merged_at must NOT overwrite it.
        await upsert_merged_pr("o/r", 7, merged_at="2026-07-13T00:00:00Z")
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got.merged_at == "2026-07-12T00:00:00Z"

    asyncio.run(run())


def test_upsert_normalizes_plus_offset_to_z():
    async def run():
        # datetime.isoformat() form (+00:00) must be stored trailing-Z.
        iso = datetime(2026, 7, 12, tzinfo=timezone.utc).isoformat()  # ...+00:00
        assert iso.endswith("+00:00")
        await upsert_merged_pr(
            "o/r", 7, merged_at=iso, created_at=iso, first_green_at=iso,
            enqueued_at=iso,
        )
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            for val in (got.merged_at, got.created_at, got.first_green_at, got.enqueued_at):
                assert val is not None and val.endswith("Z") and "+00:00" not in val

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# mark_env_live — idempotent, qa/prod name mapping
# --------------------------------------------------------------------------- #


def test_mark_env_live_maps_qa_and_prod():
    async def run():
        await upsert_merged_pr("o/r", 7, merged_at="2026-07-12T00:00:00Z")
        await mark_env_live("o/r", 7, "qa", "2026-07-12T01:00:00Z")
        await mark_env_live("o/r", 7, "prod", "2026-07-12T02:00:00Z")
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got.qa_live_at == "2026-07-12T01:00:00Z"
            assert got.prod_live_at == "2026-07-12T02:00:00Z"

    asyncio.run(run())


def test_mark_env_live_production_alias_maps_to_prod():
    async def run():
        await upsert_merged_pr("o/r", 7, merged_at="2026-07-12T00:00:00Z")
        await mark_env_live("o/r", 7, "production", "2026-07-12T02:00:00Z")
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got.prod_live_at == "2026-07-12T02:00:00Z"
            assert got.qa_live_at is None

    asyncio.run(run())


def test_mark_env_live_first_write_wins():
    async def run():
        await upsert_merged_pr("o/r", 7, merged_at="2026-07-12T00:00:00Z")
        await mark_env_live("o/r", 7, "qa", "2026-07-12T01:00:00Z")
        # Later ancestry re-check must NOT overwrite the earliest live timestamp.
        await mark_env_live("o/r", 7, "qa", "2026-07-12T05:00:00Z")
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got.qa_live_at == "2026-07-12T01:00:00Z"

    asyncio.run(run())


def test_mark_env_live_normalizes_plus_offset():
    async def run():
        await upsert_merged_pr("o/r", 7, merged_at="2026-07-12T00:00:00Z")
        iso = datetime(2026, 7, 12, 1, tzinfo=timezone.utc).isoformat()
        await mark_env_live("o/r", 7, "qa", iso)
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got.qa_live_at.endswith("Z") and "+00:00" not in got.qa_live_at

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# timelines_for — camelCase payload dicts
# --------------------------------------------------------------------------- #


def test_timelines_for_camelcase_keys():
    async def run():
        await upsert_merged_pr(
            "o/r", 7, merged_at="2026-07-12T00:00:00Z",
            created_at="2026-07-11T00:00:00Z",
            first_green_at="2026-07-11T12:00:00Z",
            enqueued_at="2026-07-11T23:00:00Z",
        )
        await mark_env_live("o/r", 7, "qa", "2026-07-12T01:00:00Z")
        out = await timelines_for("o/r", [7, 8])
        assert set(out) == {7}
        assert out[7] == {
            "createdAt": "2026-07-11T00:00:00Z",
            "firstGreenAt": "2026-07-11T12:00:00Z",
            "enqueuedAt": "2026-07-11T23:00:00Z",
            "mergedAt": "2026-07-12T00:00:00Z",
            "qaLiveAt": "2026-07-12T01:00:00Z",
            "prodLiveAt": None,
        }

    asyncio.run(run())


def test_timelines_for_empty_numbers():
    async def run():
        assert await timelines_for("o/r", []) == {}

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# tracked_merged — retention window, Z-form comparison
# --------------------------------------------------------------------------- #


def test_tracked_merged_respects_retention_window():
    async def run():
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        # Inside 7-day window.
        await upsert_merged_pr("o/r", 1, merged_at="2026-07-10T00:00:00Z")
        await upsert_merged_pr("o/r", 2, merged_at="2026-07-06T00:00:00Z")
        # Outside window (>7 days old).
        await upsert_merged_pr("o/r", 3, merged_at="2026-07-01T00:00:00Z")
        # Other repo — excluded.
        await upsert_merged_pr("o/x", 9, merged_at="2026-07-11T00:00:00Z")
        rows = await tracked_merged("o/r", retention_days=7, now=now)
        nums = [r.number for r in rows]
        assert nums == [1, 2]  # newest-first, out-of-window + other repo excluded

    asyncio.run(run())


def test_tracked_merged_z_form_comparison_with_offset_now():
    async def run():
        await upsert_merged_pr("o/r", 1, merged_at="2026-07-10T00:00:00Z")
        # now passed as a tz-aware datetime whose isoformat() is +00:00 — the
        # window boundary must still compare lexicographically against Z-form
        # stored timestamps ('+' sorts below digits, so a raw +00:00 boundary
        # would wrongly include everything).
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        rows = await tracked_merged("o/r", retention_days=1, now=now)
        assert [r.number for r in rows] == []  # 07-10 is >1 day before 07-12

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Poller integration — open-ci → green → merged produces a MergedPr row
# --------------------------------------------------------------------------- #


def _pop_resp(number: int, state: str, *, rollup: str, merged_at=None,
              merge_status="CLEAN", queued: bool = False):
    """Population-query response for one PR in one repo."""
    node = {
        "number": number, "url": f"u{number}", "title": "t", "state": state,
        "isDraft": False, "updatedAt": "2026-07-12T00:00:00Z",
        "mergedAt": merged_at, "createdAt": "2026-07-11T00:00:00Z",
        "mergeCommit": {"oid": "deadbeef"} if merged_at else None,
        "headRefOid": "x", "mergeStateStatus": merge_status,
        "mergeQueueEntry": {"state": "QUEUED"} if queued else None,
        "commits": {"nodes": [{"commit": {"statusCheckRollup":
            {"state": rollup, "contexts": {"nodes": []}}}}]},
    }
    if state == "MERGED":
        merged_nodes = [node]
        open_nodes: list = []
    else:
        merged_nodes = []
        open_nodes = [node]
    return {"data": {
        "repo": {"deployments": {"nodes": []},
                 "pullRequests": {"nodes": open_nodes}},
        "merged": {"nodes": merged_nodes},
    }}


def test_poller_captures_first_green_then_merge():
    async def run():
        # Cycle scripts: 1) open, CI pending  2) open, passing+ready (green
        # transition)  3) merged.
        cycles = [
            _pop_resp(7, "OPEN", rollup="PENDING", merge_status="BLOCKED"),
            _pop_resp(7, "OPEN", rollup="SUCCESS", merge_status="CLEAN"),
            _pop_resp(7, "MERGED", rollup="SUCCESS",
                      merged_at="2026-07-12T03:00:00Z"),
        ]
        idx = {"i": 0}

        def qfn(repo: str) -> dict:
            return cycles[idx["i"]]

        p = PrPopulationPoller(query_fn=qfn)
        for i in range(3):
            idx["i"] = i
            await p.refresh_once(["o/r"])

        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
        assert got is not None
        assert got.merged_at == "2026-07-12T03:00:00Z"
        assert got.merge_commit_sha == "deadbeef"
        assert got.created_at == "2026-07-11T00:00:00Z"
        # first_green observed on the ci→ready transition in cycle 2.
        assert got.first_green_at is not None

    asyncio.run(run())


def test_poller_merge_without_prior_green_leaves_first_green_null():
    async def run():
        # PR first observed already merged — no live transition captured.
        cycles = [
            _pop_resp(8, "MERGED", rollup="SUCCESS",
                      merged_at="2026-07-12T03:00:00Z"),
        ]
        p = PrPopulationPoller(query_fn=lambda repo: cycles[0])
        await p.refresh_once(["o/r"])
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 8))
        assert got is not None
        assert got.first_green_at is None

    asyncio.run(run())


def test_poller_ci_to_parked_is_not_green():
    """SPEC §2: firstGreenAt records only on ci → ready|queue. A PR whose CI
    FAILS (ci → parked/ci-failed) must NOT record a green waypoint, even if it
    later merges (e.g. admin-merged)."""
    async def run():
        cycles = [
            _pop_resp(9, "OPEN", rollup="PENDING", merge_status="BLOCKED"),  # ci
            _pop_resp(9, "OPEN", rollup="FAILURE", merge_status="BLOCKED"),  # parked
            _pop_resp(9, "MERGED", rollup="FAILURE",
                      merged_at="2026-07-12T04:00:00Z"),
        ]
        idx = {"i": 0}

        def qfn(repo: str) -> dict:
            return cycles[idx["i"]]

        p = PrPopulationPoller(query_fn=qfn)
        for i in range(3):
            idx["i"] = i
            await p.refresh_once(["o/r"])

        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 9))
        assert got is not None
        assert got.first_green_at is None

    asyncio.run(run())
