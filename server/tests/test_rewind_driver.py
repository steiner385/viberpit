"""D-9 driver rewind adapter: the raw control requests the Claude driver sends for
``rewind_files`` (with ``dry_run``) and ``rewind_conversation``, and the mapping of
every CLI refusal onto one ``REWIND_CODES`` code.

The fake control channel stands in for the SDK's ``Query._send_control_request``
(the internal method the SDK's own ``rewind_files()`` uses). The response payloads
are the ones the installed CLI 2.1.317 answered in the D-9 spike
(``docs/superpowers/specs/2026-11-03-d9-rewind-verification.md`` §2–§5).
"""
from __future__ import annotations

import asyncio
import inspect
import logging

import pytest
from claude_agent_sdk._errors import CLIConnectionError

from server.sessions import rewind
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.rewind import (REWIND_BUSY, REWIND_CODES, REWIND_FAILED,
                                    REWIND_NO_CHECKPOINT, REWIND_NOT_USER_MESSAGE,
                                    REWIND_SPLITS_TOOL_CALL, REWIND_STALE_TARGET,
                                    REWIND_OUTCOME_UNKNOWN, REWIND_TARGET_NOT_FOUND,
                                    REWIND_UNAVAILABLE,
                                    ConversationRewind, RewindError, RewindPreview,
                                    map_cli_error)
from server.tests.fakes.fake_sdk import FakeSDKClient

U1 = "21203c3a-0000-4000-8000-000000000001"
U2 = "0497e020-0000-4000-8000-000000000002"
U3 = "2ff905f3-0000-4000-8000-000000000003"

# Spike §5 / contract-test replies, verbatim.
DRY_U2 = {"canRewind": True, "filesChanged": ["/tmp/p/a.txt"], "insertions": 1, "deletions": 1}
DRY_NO_CHECKPOINT = {"canRewind": False, "error": "No file checkpoint found for this message."}
DRY_NOT_ENABLED = {"canRewind": False, "error": "File rewinding is not enabled."}
REAL_OK = {"canRewind": True, "skippedLinks": 0}
CONV_OK = {"rewound": True, "targetMessageUuid": U2,
           "prefillText": "Use the Edit tool on a.txt", "precedingAssistantUuid": "9346e4fc"}
CONV_FIRST = {"rewound": True, "targetMessageUuid": U1, "prefillText": "Read a.txt",
              "precedingAssistantUuid": None}


def _refusal(reason: str, error: str) -> dict:
    return {"rewound": False, "prefillText": None, "precedingAssistantUuid": None,
            "error": error, "reason": reason}


class FakeControl:
    """The SDK ``Query`` control channel: records each request, answers from a
    script. An ``Exception`` in the script is raised, as the SDK raises a control
    ``error`` response."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent: list[dict] = []

    async def _send_control_request(self, request: dict, timeout: float = 60.0) -> dict:
        self.sent.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _driver(control: FakeControl | None, registry: PermissionRegistry | None = None,
            clients: list | None = None):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None):
        client = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        if control is not None:
            client._query = control
        if clients is not None:
            clients.append(client)
        return client
    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=make, registry=registry or PermissionRegistry(),
                             identity="me")


def _run(coro_fn):
    async def go():
        return await coro_fn()
    return asyncio.run(go())


# ---- request shapes ------------------------------------------------------------

def test_dry_run_sends_exact_request_and_returns_reply():
    control = FakeControl(DRY_U2)

    async def go():
        d = _driver(control)
        await d.start()
        try:
            return await d.rewind_files(U2, dry_run=True)
        finally:
            await d.aclose()
    assert _run(go) == DRY_U2
    assert control.sent == [{"subtype": "rewind_files", "user_message_id": U2, "dry_run": True}]


def test_real_file_rewind_sends_sdk_shape_without_dry_run():
    """The real run mirrors the SDK's own ``rewind_files`` payload (no ``dry_run``)."""
    control = FakeControl(REAL_OK)

    async def go():
        d = _driver(control)
        await d.start()
        try:
            return await d.rewind_files(U2, dry_run=False)
        finally:
            await d.aclose()
    assert _run(go) == REAL_OK
    assert control.sent == [{"subtype": "rewind_files", "user_message_id": U2}]


def test_conversation_rewind_sends_target_and_last_seen():
    control = FakeControl(CONV_OK)

    async def go():
        d = _driver(control)
        await d.start()
        try:
            return await d.rewind_conversation(U2, last_seen=U3)
        finally:
            await d.aclose()
    assert _run(go) == CONV_OK
    assert control.sent == [{"subtype": "rewind_conversation", "target_message_uuid": U2,
                             "last_seen_user_message_uuid": U3}]


def test_conversation_rewind_omits_absent_last_seen():
    control = FakeControl(CONV_FIRST)

    async def go():
        d = _driver(control)
        await d.start()
        try:
            return await d.rewind_conversation(U1, last_seen=None)
        finally:
            await d.aclose()
    assert _run(go) == CONV_FIRST
    assert control.sent == [{"subtype": "rewind_conversation", "target_message_uuid": U1}]


# ---- refusals from the CLI -----------------------------------------------------

@pytest.mark.parametrize("reply,code", [
    (DRY_NO_CHECKPOINT, REWIND_NO_CHECKPOINT),
    (DRY_NOT_ENABLED, REWIND_NO_CHECKPOINT),
])
def test_dry_run_can_rewind_false_raises_mapped_code(reply, code):
    control = FakeControl(reply)

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_files(U2, dry_run=True)
            return ei.value
        finally:
            await d.aclose()
    err = _run(go)
    assert err.code == code
    assert err.error_data["code"] == code


def test_real_run_control_error_maps_to_no_checkpoint():
    """Without ``dry_run`` the CLI turns ``canRewind:false`` into a control error,
    which the SDK raises as a bare ``Exception(error)``."""
    control = FakeControl(Exception("No file checkpoint found for this message."))

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_files(U2, dry_run=False)
            return ei.value
        finally:
            await d.aclose()
    assert _run(go).code == REWIND_NO_CHECKPOINT


def test_conversation_refusal_raises_mapped_code_with_reason():
    control = FakeControl(_refusal("stale_target", "stale target"))

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_conversation(U2, last_seen=None)
            return ei.value
        finally:
            await d.aclose()
    err = _run(go)
    assert err.code == REWIND_STALE_TARGET
    assert err.error_data == {"code": REWIND_STALE_TARGET, "reason": "stale_target"}


def test_unexpected_exception_maps_to_failed():
    control = FakeControl(Exception("Control request timeout: rewind_files"))

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_files(U2, dry_run=True)
            return ei.value
        finally:
            await d.aclose()
    assert _run(go).code == REWIND_FAILED


# ---- driver-side guards --------------------------------------------------------

def test_refuses_busy_while_turn_runs_and_sends_nothing():
    control = FakeControl(DRY_U2, CONV_OK)

    async def go():
        d = _driver(control)
        await d.start()
        d._turn_active = True
        try:
            codes = []
            for call in (lambda: d.rewind_files(U2, dry_run=True),
                         lambda: d.rewind_conversation(U2, last_seen=U3)):
                with pytest.raises(RewindError) as ei:
                    await call()
                codes.append(ei.value.code)
            return codes
        finally:
            d._turn_active = False
            await d.aclose()
    assert _run(go) == [REWIND_BUSY, REWIND_BUSY]
    assert control.sent == []


def test_refuses_busy_while_permission_card_pending():
    control = FakeControl(DRY_U2)
    reg = PermissionRegistry()
    reg.pending = lambda session_id: [{"tool": "Edit"}]  # type: ignore[method-assign]

    async def go():
        d = _driver(control, reg)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_files(U2, dry_run=True)
            return ei.value.code
        finally:
            await d.aclose()
    assert _run(go) == REWIND_BUSY
    assert control.sent == []


def test_unavailable_before_start():
    d = _driver(FakeControl())

    async def go():
        with pytest.raises(RewindError) as ei:
            await d.rewind_files(U2, dry_run=True)
        return ei.value.code
    assert _run(go) == REWIND_UNAVAILABLE


def test_unavailable_when_client_has_no_control_channel():
    async def go():
        d = _driver(None)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_conversation(U2, last_seen=U3)
            return ei.value.code
        finally:
            await d.aclose()
    assert _run(go) == REWIND_UNAVAILABLE


def test_refusals_log_at_info_never_warning(caplog):
    control = FakeControl(_refusal("turn_running", "turn running"))

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError):
                await d.rewind_conversation(U2, last_seen=U3)
        finally:
            await d.aclose()
    with caplog.at_level(logging.DEBUG, logger="server.sessions.claude_driver"):
        _run(go)
    rewind_records = [r for r in caplog.records if "rewind" in r.getMessage()]
    assert rewind_records, "the refusal is logged"
    assert all(r.levelno == logging.INFO for r in rewind_records)


# ---- map_cli_error -------------------------------------------------------------

@pytest.mark.parametrize("reason,code", [
    ("turn_running", REWIND_BUSY),
    ("commands_queued", REWIND_BUSY),
    ("prompt_pending", REWIND_BUSY),
    ("stale_target", REWIND_STALE_TARGET),
    ("unseen_later_turn", REWIND_STALE_TARGET),
    ("state_changed", REWIND_STALE_TARGET),
    ("target_not_found", REWIND_TARGET_NOT_FOUND),
    ("target_splits_tool_call", REWIND_SPLITS_TOOL_CALL),
    ("delivered_poll_events_in_range", REWIND_SPLITS_TOOL_CALL),
    ("poll_tool_result_target", REWIND_NOT_USER_MESSAGE),
    ("persist_failed", REWIND_FAILED),
    ("usage_limited", REWIND_FAILED),
    ("expired", REWIND_FAILED),
    ("some_future_reason", REWIND_FAILED),
])
def test_every_cli_reason_maps_to_a_code(reason, code):
    err = map_cli_error(_refusal(reason, reason.replace("_", " ")))
    assert err.code == code
    assert err.error_data == {"code": code, "reason": reason}


def test_cli_reason_table_covers_binary_vocabulary():
    """Every reason the 2.1.317 binary's ``bw`` table can send (spike §3 / handler)
    has an explicit entry, so a new one is noticed rather than silently FAILED."""
    binary_reasons = {"commands_queued", "prompt_pending", "turn_running", "target_not_found",
                      "stale_target", "unseen_later_turn", "poll_tool_result_target",
                      "target_splits_tool_call", "delivered_poll_events_in_range",
                      "persist_failed", "state_changed", "usage_limited", "expired"}
    assert set(rewind.CLI_REASON_CODES) == binary_reasons


@pytest.mark.parametrize("reply,code", [
    (DRY_NO_CHECKPOINT, REWIND_NO_CHECKPOINT),
    (DRY_NOT_ENABLED, REWIND_NO_CHECKPOINT),
    ({"error": "Error: --rewind-files requires a user message UUID, but x is not a user "
               "message in this session"}, REWIND_NOT_USER_MESSAGE),
    ({"canRewind": False}, REWIND_FAILED),
    ({}, REWIND_FAILED),
])
def test_error_text_maps_to_a_code(reply, code):
    err = map_cli_error(reply)
    assert err.code == code
    assert err.error_data == {"code": code}


def test_codes_table_is_complete():
    assert REWIND_CODES == frozenset({
        "REWIND_UNAVAILABLE", "REWIND_BUSY", "REWIND_NO_CHECKPOINT", "REWIND_STALE_TARGET",
        "REWIND_TARGET_NOT_FOUND", "REWIND_SPLITS_TOOL_CALL", "REWIND_NOT_USER_MESSAGE",
        "REWIND_TOO_LARGE", "REWIND_UNDO_EXPIRED", "REWIND_FAILED"})
    assert set(rewind.CLI_REASON_CODES.values()) <= REWIND_CODES


def test_rewind_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        RewindError("NOPE")


# ---- result dataclasses --------------------------------------------------------

def test_preview_from_dry_run():
    assert RewindPreview.from_cli(DRY_U2) == RewindPreview(
        files=("/tmp/p/a.txt",), insertions=1, deletions=1, checkpoint_available=True)


def test_preview_without_checkpoint():
    assert RewindPreview.no_checkpoint() == RewindPreview(
        files=(), insertions=0, deletions=0, checkpoint_available=False)


def test_conversation_rewind_from_reply():
    assert ConversationRewind.from_cli(CONV_OK) == ConversationRewind(
        prefill="Use the Edit tool on a.txt", preceding_uuid="9346e4fc")
    assert ConversationRewind.from_cli(CONV_FIRST).preceding_uuid is None


# ---- the SDK seam the adapter relies on ----------------------------------------

def test_sdk_rewind_files_uses_the_control_channel_the_adapter_uses():
    """Constitution I: the adapter reaches the CLI through the same internal method
    the SDK's own ``rewind_files()`` uses. If the SDK renames it, this fails loudly."""
    from claude_agent_sdk._internal.query import Query
    src = inspect.getsource(Query.rewind_files)
    assert "self._send_control_request(" in src
    params = inspect.signature(Query._send_control_request).parameters
    assert list(params)[:2] == ["self", "request"]
    assert "timeout" in params


# ---- outcome unknown: a state-changing request with no reply ------------------

def _timeout_exc(subtype: str) -> Exception:
    """What the SDK raises on a control timeout: a bare Exception from TimeoutError."""
    try:
        try:
            raise TimeoutError
        except TimeoutError as e:
            raise Exception(f"Control request timeout: {subtype}") from e
    except Exception as exc:
        return exc


@pytest.mark.parametrize("call,exc", [
    (lambda d: d.rewind_files(U2, dry_run=False), _timeout_exc("rewind_files")),
    (lambda d: d.rewind_conversation(U2, last_seen=U3), _timeout_exc("rewind_conversation")),
    (lambda d: d.rewind_files(U2, dry_run=False), CLIConnectionError("pipe closed")),
    (lambda d: d.rewind_conversation(U2, last_seen=U3), OSError("reader died")),
])
def test_no_reply_to_a_mutating_request_is_outcome_unknown(call, exc):
    control = FakeControl(exc)

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await call(d)
            return ei.value
        finally:
            await d.aclose()
    err = _run(go)
    assert err.code == REWIND_FAILED
    assert err.error_data == {"code": REWIND_FAILED, "reason": REWIND_OUTCOME_UNKNOWN}


def test_dry_run_timeout_is_plain_failed():
    control = FakeControl(_timeout_exc("rewind_files"))

    async def go():
        d = _driver(control)
        await d.start()
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_files(U2, dry_run=True)
            return ei.value
        finally:
            await d.aclose()
    assert _run(go).error_data == {"code": REWIND_FAILED}


# ---- rewind_hold ---------------------------------------------------------------

async def _settle(d, client, n_sent: int) -> None:
    """Wait (bounded) until the client saw ``n_sent`` prompts and the turn settled."""
    for _ in range(200):
        if len(client.sent) >= n_sent and not d._turn_live():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"sent={client.sent!r} live={d._turn_live()}")


def test_prompt_sent_during_hold_is_queued_and_starts_after_release():
    control = FakeControl(DRY_U2, REAL_OK, CONV_OK)
    clients: list = []

    async def go():
        d = _driver(control, clients=clients)
        await d.start()
        client = clients[0]
        try:
            async with d.rewind_hold():
                await d.rewind_files(U2, dry_run=True)
                await d.send_prompt("typed during the rewind")
                assert client.sent == []
                assert [q[0] for q in d._queue] == ["typed during the rewind"]
                # The hold is not "busy": the sequence keeps going with a prompt queued.
                await d.rewind_files(U2, dry_run=False)
                await d.rewind_conversation(U2, last_seen=U3)
            await _settle(d, client, 1)
            return list(client.sent), list(d._queue)
        finally:
            await d.aclose()
    sent, queue = _run(go)
    assert len(sent) == 1 and "typed during the rewind" in sent[0]
    assert queue == []
    assert [r["subtype"] for r in control.sent] == ["rewind_files", "rewind_files",
                                                     "rewind_conversation"]


def test_hold_refused_while_a_turn_is_starting():
    async def go():
        d = _driver(FakeControl())
        await d.start()
        d._starting_turn = True
        try:
            with pytest.raises(RewindError) as ei:
                async with d.rewind_hold():
                    pass
            return ei.value.code, d._rewind_held
        finally:
            d._starting_turn = False
            await d.aclose()
    assert _run(go) == (REWIND_BUSY, False)


def test_per_call_check_refuses_while_a_turn_is_starting():
    control = FakeControl(DRY_U2)

    async def go():
        d = _driver(control)
        await d.start()
        d._starting_turn = True
        try:
            with pytest.raises(RewindError) as ei:
                await d.rewind_files(U2, dry_run=True)
            return ei.value.code
        finally:
            d._starting_turn = False
            await d.aclose()
    assert _run(go) == REWIND_BUSY
    assert control.sent == []


def test_second_hold_is_refused_busy():
    async def go():
        d = _driver(FakeControl())
        await d.start()
        try:
            async with d.rewind_hold():
                with pytest.raises(RewindError) as ei:
                    async with d.rewind_hold():
                        pass
                still_held = d._rewind_held
            return ei.value.code, still_held, d._rewind_held
        finally:
            await d.aclose()
    assert _run(go) == (REWIND_BUSY, True, False)


def test_exception_inside_hold_releases_and_drains():
    clients: list = []

    async def go():
        d = _driver(FakeControl(), clients=clients)
        await d.start()
        client = clients[0]
        try:
            with pytest.raises(RuntimeError, match="backup failed"):
                async with d.rewind_hold():
                    await d.send_prompt("queued")
                    raise RuntimeError("backup failed")
            assert d._rewind_held is False
            await _settle(d, client, 1)
            return list(client.sent), list(d._queue)
        finally:
            await d.aclose()
    sent, queue = _run(go)
    assert len(sent) == 1 and "queued" in sent[0]
    assert queue == []


def test_cancellation_inside_hold_releases_and_drains():
    clients: list = []

    async def go():
        d = _driver(FakeControl(), clients=clients)
        await d.start()
        client = clients[0]
        entered = asyncio.Event()

        async def holder():
            async with d.rewind_hold():
                await d.send_prompt("queued")
                entered.set()
                await asyncio.sleep(3600)
        try:
            task = asyncio.create_task(holder())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert d._rewind_held is False
            await _settle(d, client, 1)
            # A later hold can be taken again.
            async with d.rewind_hold():
                pass
            return list(client.sent)
        finally:
            await d.aclose()
    sent = _run(go)
    assert len(sent) == 1 and "queued" in sent[0]
