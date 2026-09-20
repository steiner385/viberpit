"""The Pipeline's `main` lane reads the deploy controller's CI verdict for the
default-branch tip — the same verdict the deploy banner shows — so the two
surfaces can never disagree about whether master is in CI (the 2026-09-11
mismatch: banner "CI pending", Pipeline "main · no signal")."""
import asyncio

from server.deploy.client import DeployControllerUnavailable
from server.pipeline.lane_health import (
    controller_deploy_snapshot,
    lane_health_ctx,
    main_lane_health,
)


def test_main_lane_health_maps_tip_verdicts_to_lane_status():
    base = {"behind": True, "targetSha": "5c8056c0dcf3", "ci": "pending"}
    assert main_lane_health({**base, "tipCi": "green"})["main"] == "green"
    assert main_lane_health({**base, "tipCi": "pending"}) == {
        "main": "amber", "tipSha": "5c8056c0dcf3", "tipCi": "pending"}
    assert main_lane_health({**base, "tipCi": "red"})["main"] == "red"
    # Cancelled-before-start never resolves on its own: watch, not broken.
    assert main_lane_health({**base, "tipCi": "unrun"})["main"] == "amber"
    # Wired but the verdict could not be read: blind, never a false green.
    assert main_lane_health({**base, "tipCi": "unavailable"})["main"] == "blind"
    assert main_lane_health({**base, "tipCi": "???"})["main"] == "blind"


def test_main_lane_health_falls_back_to_ci_for_pre_frontier_controllers():
    assert main_lane_health({"behind": True, "targetSha": "x", "ci": "red"})["main"] == "red"


def test_lane_health_ctx_keys_by_the_controller_repo():
    status = {"repo": "o/r", "deploy": {},
              "pending": {"behind": False, "targetSha": "abc", "ci": "green", "tipCi": "green"}}
    assert lane_health_ctx(status) == {"o/r": {"main": "green", "tipSha": "abc", "tipCi": "green"}}


def test_lane_health_ctx_makes_no_claim_without_a_repo_or_status():
    assert lane_health_ctx(None) == {}
    # An older controller ships no `repo`: which repo the verdict is for is unknown.
    assert lane_health_ctx({"pending": {"ci": "green", "targetSha": "x"}}) == {}
    assert lane_health_ctx({"repo": "o/r"}) == {}


def test_controller_deploy_snapshot_synthesizes_an_env_from_pending():
    status = {"repo": "o/r", "pending": {
        "behind": True, "liveSha": "abc123", "targetSha": "def456",
        "ci": "pending", "tipCi": "pending",
        "deployableSha": "abc123", "deployableCount": 3,
        "commits": [{"sha": "x"}, {"sha": "y"}, {"sha": "z"}, {"sha": "w"}],
    }}
    assert controller_deploy_snapshot(status) == {"o/r": {
        "envs": [{"name": "prod-edge", "liveSha": "abc123", "reachable": True}],
        "awaitingQa": 0,
        "awaitingProd": 3,
    }}


def test_controller_deploy_snapshot_falls_back_to_commit_count_when_nothing_deployable():
    # tip is red/pending and no older commit is a green frontier: deployableCount
    # is 0, so awaitingProd falls back to every pending commit rather than
    # under-reporting the drift as zero.
    status = {"repo": "o/r", "pending": {
        "liveSha": "abc123", "deployableCount": 0,
        "commits": [{"sha": "x"}, {"sha": "y"}],
    }}
    assert controller_deploy_snapshot(status)["o/r"]["awaitingProd"] == 2


def test_controller_deploy_snapshot_makes_no_claim_without_a_repo_or_status():
    assert controller_deploy_snapshot(None) == {}
    assert controller_deploy_snapshot({"pending": {"liveSha": "x"}}) == {}
    assert controller_deploy_snapshot({"repo": "o/r"}) == {}


def test_build_state_lane_health_survives_an_unreachable_controller():
    from server.api.pipeline import _lane_health_ctx

    async def boom() -> dict:
        raise DeployControllerUnavailable("down")

    async def ok() -> dict:
        return {"repo": "o/r", "pending": {"behind": True, "targetSha": "s",
                                           "ci": "pending", "tipCi": "pending"}}

    async def run():
        assert await _lane_health_ctx(None) == {}
        assert await _lane_health_ctx(boom) == {}
        assert (await _lane_health_ctx(ok))["o/r"]["main"] == "amber"

    asyncio.run(run())
