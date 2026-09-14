"""Ownership retries and deferred launch preserve per-session governance choices."""
from unittest.mock import patch

import pytest

from server.automations.ownership import CanonicalTarget, acquire, owned
from server.sessions.types import AUTO_ARCHIVE_OPT_OUT_CREATE
from server.tests.test_create_session_per_session_toggles import (
    _queue_params, _row, _run_with_orch,
)


@pytest.mark.parametrize("path", ["spawn", "queue", "hold"])
def test_owned_launch_and_persisted_retry_preserve_opt_outs(tmp_path, path):
    async def body(orch):
        async with acquire(
            [CanonicalTarget("I_owned_toggle", "o/r#1")], owner="backlog-runner",
            source="automatic", reason="dispatch", provenance={"origin_ref": "o/r#1"},
        ) as tx:
            token = tx.token
        kwargs = dict(
            name="owned", origin="backlog-runner", origin_ref="o/r#1",
            ownership_token=token, autopilot_paused=True,
            auto_archive_opt_out_reason=AUTO_ARCHIVE_OPT_OUT_CREATE,
        )
        with (
            patch.object(orch, "_max_active_sessions", return_value=0 if path == "queue" else 10),
            patch("server.sessions.model_policy.resolve_model_policy",
                  return_value="require_approval" if path == "hold" else "allow"),
        ):
            rec = await orch.create_session(**kwargs)
            # The token joins the durable row even on a process without the record;
            # retry defaults must not overwrite the original operator choices.
            prior = orch._records.pop(rec.id)
            try:
                retry = await orch.create_session(
                    "owned", origin="backlog-runner", origin_ref="o/r#1", ownership_token=token)
            finally:
                orch._records[rec.id] = prior
            assert retry.id == rec.id
            assert retry.autopilot_paused is True
            assert retry.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE
        if path == "queue":
            params = await _queue_params(rec.id)
            assert params["autopilot_paused"] is True
            assert params["auto_archive_opt_out_reason"] == AUTO_ARCHIVE_OPT_OUT_CREATE
            assert await orch.maybe_start_queued() == 1
        elif path == "hold":
            orch._pending_approvals.clear()  # row-only restart recovery
            params = await orch._rebuild_hold_kwargs(rec.id)
            assert params["autopilot_paused"] is True
            assert params["auto_archive_opt_out_reason"] == AUTO_ARCHIVE_OPT_OUT_CREATE
            await orch.resolve_model_approval(rec.id, "approve", identity="test")
        live = orch._records[rec.id]
        assert rec.id in orch._drivers
        row = await _row(rec.id)
        for value in (live, row):
            assert value.autopilot_paused is True
            assert value.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE
        async with owned(token) as tx:
            assert tx.operation.session_id == rec.id
            assert tx.operation.execution_generation > 0

    _run_with_orch(tmp_path, body)
