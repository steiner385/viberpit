"""An unmarked map-diff turn finishes only on independently verified evidence."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from server.tests.test_completion_releases_driver import _build_orch, _drain, _FakeDriver


async def _map_orch(tmp_path):
    orch = await _build_orch(
        tmp_path, origin="pm-onboard", origin_ref="viberpit:map-diff:offline-mode",
        repo="steiner385/viberpit")
    orch._drivers["s1"] = _FakeDriver()
    orch._completion_open_question = AsyncMock(return_value=None)
    orch._pm_onboard_observe_map_diff_not_needed = AsyncMock(return_value=True)
    orch._read_auto_archive_enabled = AsyncMock(return_value=False)
    orch._maybe_apply_auto_name = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_unmarked_map_diff_turn_is_verified_and_releases_driver(tmp_path):
    orch = await _map_orch(tmp_path)
    driver = orch._drivers["s1"]
    await orch._persist_status("s1", "done")
    await _drain(orch)
    await _drain(orch)
    rec = orch.record("s1")
    assert rec.status == "complete"
    assert rec.pr_resolution_source == "observed"
    assert driver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["waiting_question", "waiting_permission", "waiting_plan"])
async def test_unmarked_map_diff_preserves_open_holds(tmp_path, hold):
    orch = await _map_orch(tmp_path)
    orch._completion_open_question.return_value = hold
    await orch._persist_status("s1", "done")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert orch.record("s1").pr_resolution is None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_unmarked_map_diff_unverified_keeps_driver(tmp_path):
    orch = await _map_orch(tmp_path)
    orch._pm_onboard_observe_map_diff_not_needed.return_value = False
    await orch._persist_status("s1", "done")
    await _drain(orch)
    assert orch.record("s1").status == "done"
    assert orch.record("s1").pr_resolution is None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_unmarked_map_diff_respects_manual_opt_out(tmp_path):
    orch = await _map_orch(tmp_path)
    orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
    await orch._persist_status("s1", "done")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert orch.record("s1").status == "done"


@pytest.mark.asyncio
async def test_new_turn_during_repository_observation_is_not_completed(tmp_path):
    orch = await _map_orch(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(rec):
        entered.set()
        await release.wait()
        return True

    orch._pm_onboard_observe_map_diff_not_needed.side_effect = observe
    await orch._persist_status("s1", "done")
    await asyncio.wait_for(entered.wait(), timeout=2)
    await orch._persist_status("s1", "in_progress")
    release.set()
    await _drain(orch)
    assert orch.record("s1").status == "in_progress"
    assert orch.record("s1").pr_resolution is None
    assert not orch._drivers["s1"].closed


@pytest.mark.asyncio
async def test_question_check_failure_leaves_unmarked_turn_alone(tmp_path):
    orch = await _map_orch(tmp_path)
    orch._completion_open_question.side_effect = RuntimeError("unavailable")
    await orch._persist_status("s1", "done")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_not_awaited()
    assert orch.record("s1").status == "done"


@pytest.mark.asyncio
async def test_human_archive_persists_disposition_before_teardown(tmp_path):
    orch = await _map_orch(tmp_path)
    driver = orch._drivers["s1"]

    async def observe(rec, *, disposition_source=None):
        assert orch._drivers["s1"] is driver
        assert disposition_source == "human-archive"
        return True

    orch._pm_onboard_observe_map_diff_not_needed.side_effect = observe
    await orch.archive_session("s1")
    await _drain(orch)
    orch._pm_onboard_observe_map_diff_not_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_human_disposition_write_prevents_loss_of_session(tmp_path):
    orch = await _map_orch(tmp_path)
    orch._pm_onboard_observe_map_diff_not_needed.side_effect = RuntimeError("DB unavailable")
    with pytest.raises(RuntimeError, match="DB unavailable"):
        await orch.archive_session("s1")
    assert orch.record("s1") is not None
    assert not orch._drivers["s1"].closed
