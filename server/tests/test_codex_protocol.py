"""Codex app-server JSONL client: framing, dispatch, fail-closed, and redaction.

Every test drives the real client against the in-process fake app-server in
`server/tests/fakes/fake_codex_app_server.py` over the same JSONL transport the
daemon uses, so framing and dispatch are exercised end to end without a process.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from server.codex import protocol, redaction, schema
from server.codex.redaction import REDACTED, Redacted
from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer

pytestmark = pytest.mark.asyncio

JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjY3N3aXRjaCJ9.c2lnbmF0dXJl"
ACCOUNT_ID = "acct-0123456789"


def _client(fake: FakeCodexAppServer, **kwargs: Any) -> protocol.CodexAppServerClient:
    kwargs.setdefault("external_auth_enabled", True)
    kwargs.setdefault("request_timeout_s", 1.0)
    return protocol.CodexAppServerClient(fake.transport, **kwargs)


async def _ready(
    fake: FakeCodexAppServer, **kwargs: Any
) -> protocol.CodexAppServerClient:
    """Return a started, initialized client."""
    client = _client(fake, **kwargs)
    await client.start()
    await asyncio.gather(client.initialize(), fake.serve_initialize())
    return client


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        fields = getattr(record, "fields", None)
        if fields is not None:
            parts.append(json.dumps(fields, default=str))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# initialize / initialized ordering
# ---------------------------------------------------------------------------


async def test_initialize_advertises_experimental_api_only_with_external_auth() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=True)
        try:
            frame = fake.client_frames[0]
            assert frame["method"] == "initialize"
            assert frame["params"]["capabilities"]["experimentalApi"] is True
        finally:
            await client.aclose()

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=False)
        try:
            assert (
                fake.client_frames[0]["params"]["capabilities"]["experimentalApi"] is False
            )
        finally:
            await client.aclose()


async def test_initialized_notification_follows_the_initialize_response() -> None:
    async with FakeCodexAppServer() as fake:
        client = _client(fake)
        await client.start()
        try:
            task = asyncio.create_task(client.initialize())
            request = await fake.receive()
            assert request["method"] == "initialize"
            # Nothing may be sent between the request and its response.
            assert fake.client_frames == [request]
            fake.respond(request["id"], fake.default_initialize_response())
            response = await task
            assert response.platform_os == "linux"
            notification = await fake.receive()
            assert notification["method"] == "initialized"
            assert "id" not in notification
        finally:
            await client.aclose()


async def test_initialize_does_not_log_peer_controlled_platform_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with FakeCodexAppServer() as fake:
        client = _client(fake)
        await client.start()
        try:
            with caplog.at_level(logging.INFO, logger="server.codex.protocol"):
                task = asyncio.create_task(client.initialize())
                request = await fake.receive()
                response = fake.default_initialize_response()
                response["platformOs"] = "prompt=/private/account-project"
                fake.respond(request["id"], response)
                await task
            assert "prompt=/private/account-project" not in _log_text(caplog)
        finally:
            await client.aclose()


async def test_calls_before_initialize_fail_with_a_typed_error() -> None:
    async with FakeCodexAppServer() as fake:
        client = _client(fake)
        await client.start()
        try:
            with pytest.raises(protocol.CodexNotInitializedError):
                await client.read_account()
            with pytest.raises(protocol.CodexNotInitializedError):
                await client.list_models()
            assert fake.client_frames == []
        finally:
            await client.aclose()


async def test_unsupported_client_request_methods_are_refused() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            with pytest.raises(schema.CodexSchemaError):
                await client.call("thread/start", {})
            assert len(fake.client_frames) == 2  # initialize + initialized only
        finally:
            await client.aclose()


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("account/read", {"accessToken": JWT}),
        ("account/rateLimits/read", {"prompt": "do the thing"}),
        ("model/list", {"cursor": None, "command": ["sh", "-c", "secret"]}),
        (
            "account/login/start",
            {
                "type": "chatgptAuthTokens",
                "accessToken": JWT,
                "chatgptAccountId": ACCOUNT_ID,
                "prompt": "smuggled",
            },
        ),
    ],
)
async def test_raw_calls_reject_unvalidated_or_smuggled_fields(
    method: str, params: dict[str, Any]
) -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            before = len(fake.client_frames)
            with pytest.raises(schema.CodexSchemaError):
                await client.call(method, params)
            assert len(fake.client_frames) == before
        finally:
            await client.aclose()


async def test_raw_initialized_notification_rejects_payload_fields() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            before = len(fake.client_frames)
            with pytest.raises(schema.CodexSchemaError):
                await client.notify("initialized", {"accessToken": JWT})
            assert len(fake.client_frames) == before
        finally:
            await client.aclose()


async def test_raw_initialize_cannot_enable_external_auth_on_a_disabled_client() -> None:
    async with FakeCodexAppServer() as fake:
        client = _client(fake, external_auth_enabled=False)
        await client.start()
        try:
            with pytest.raises(protocol.CodexExternalAuthDisabled):
                await client.call(
                    "initialize",
                    {
                        "clientInfo": {"name": "ccswitch", "version": "1"},
                        "capabilities": {
                            "experimentalApi": True,
                            "requestAttestation": False,
                        },
                    },
                )
            assert fake.client_frames == []
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Typed calls
# ---------------------------------------------------------------------------


async def test_typed_wrappers_parse_their_supported_responses() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.rule(
                "account/read",
                result={
                    "requiresOpenaiAuth": True,
                    "account": {"type": "chatgpt", "email": None, "planType": "pro"},
                },
            )
            fake.rule(
                "account/rateLimits/read",
                result={"rateLimits": {"primary": {"usedPercent": 11}}},
            )
            fake.rule("model/list", result={"data": [], "nextCursor": None})
            fake.rule("account/login/start", result={"type": "chatgptAuthTokens"})
            fake.start_autopilot()

            account = await client.read_account()
            assert isinstance(account.account, schema.ChatgptAccount)
            limits = await client.read_rate_limits()
            assert limits.rate_limits.primary is not None
            assert (await client.list_models()).data == ()
            login = await client.login_with_external_tokens(
                schema.ExternalAuthLoginParams(
                    access_token=Redacted(JWT),
                    chatgpt_account_id=Redacted(ACCOUNT_ID),
                )
            )
            assert login.type == "chatgptAuthTokens"
            sent = fake.frames_for("account/login/start")[0]
            assert sent["params"]["accessToken"] == JWT
        finally:
            await client.aclose()


async def test_direct_supported_call_returns_only_a_safe_typed_dto() -> None:
    secret = "plain-account-id prompt=private cwd=/private/project"
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.call("account/read", {}))
            request = await fake.expect("account/read")
            fake.respond(
                request["id"],
                {
                    "requiresOpenaiAuth": False,
                    "account": {
                        "type": "chatgpt",
                        "email": "owner@example.test",
                        "planType": "pro",
                    },
                    "unknownNativeContent": secret,
                },
            )
            result = await task
            assert isinstance(result, schema.GetAccountResponse)
            rendered = repr(result)
            assert "owner@example.test" not in rendered
            assert secret not in rendered
        finally:
            await client.aclose()


async def test_direct_supported_call_rejects_a_malformed_response() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.call("account/read", {}))
            request = await fake.expect("account/read")
            fake.respond(
                request["id"],
                {"requiresOpenaiAuth": "yes", "prompt": "private native content"},
            )
            with pytest.raises(schema.CodexSchemaError):
                await task
        finally:
            await client.aclose()


async def test_request_ids_are_unique_and_monotonic() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.rule("model/list", result={"data": []})
            fake.start_autopilot()
            for _ in range(3):
                await client.list_models()
            ids = [f["id"] for f in fake.client_frames if f.get("method") == "model/list"]
            assert ids == sorted(ids)
            assert len(set(ids)) == len(ids)
            assert all(type(i) is int for i in ids)
        finally:
            await client.aclose()


async def test_boolean_ids_never_alias_a_pending_integer_id() -> None:
    """`bool` subclasses `int`, so `{"id": true}` must not resolve pending id 1.

    Id 1 is always the `initialize` request, which is precisely when a `True` id
    on the wire would hash to a live pending entry.
    """
    async with FakeCodexAppServer() as fake:
        client = _client(fake)
        await client.start()
        try:
            task = asyncio.create_task(client.initialize())
            request = await fake.receive()
            assert request["method"] == "initialize"
            assert request["id"] == 1
            fake.send({"id": True, "result": fake.default_initialize_response()})
            fake.send({"id": False, "result": fake.default_initialize_response()})
            await fake.drained()
            assert not task.done()
            assert client.pending_request_count == 1
            assert client.counters.orphan_responses == 2
            fake.respond(request["id"], fake.default_initialize_response())
            assert (await task).platform_os == "linux"
            assert (await fake.receive())["method"] == "initialized"
        finally:
            await client.aclose()


async def test_interleaved_responses_resolve_their_own_requests() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            models = asyncio.create_task(client.list_models())
            account = asyncio.create_task(client.read_account())
            limits = asyncio.create_task(client.read_rate_limits())
            model_frame = await fake.expect("model/list")
            account_frame = await fake.expect("account/read")
            limit_frame = await fake.expect("account/rateLimits/read")

            fake.respond(limit_frame["id"], {"rateLimits": {"primary": {"usedPercent": 5}}})
            fake.respond(account_frame["id"], {"requiresOpenaiAuth": False})
            fake.respond(model_frame["id"], {"data": []})

            assert (await limits).rate_limits.primary is not None
            assert (await account).requires_openai_auth is False
            assert (await models).data == ()
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


async def test_partial_lines_are_reassembled_before_dispatch() -> None:
    """A frame split across three writes must dispatch once, at the terminator."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            lines_before = fake.transport.lines_read
            task = asyncio.create_task(client.list_models())
            request = await fake.expect("model/list")
            payload = json.dumps({"id": request["id"], "result": {"data": []}})
            fake.send_raw(payload[:5].encode())
            fake.send_raw(payload[5:].encode())
            # Give the reader real time to run: it must still be blocked mid-line,
            # having consumed no line and resolved nothing.
            await asyncio.sleep(0.05)
            assert fake.transport.lines_read == lines_before
            assert client.pending_request_count == 1
            assert not task.done()

            fake.send_raw(b"\n")
            assert (await task).data == ()
            assert fake.transport.lines_read == lines_before + 1
            assert client.counters.malformed_frames == 0
            assert client.pending_request_count == 0
        finally:
            await client.aclose()


async def test_malformed_json_is_counted_and_skipped() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.list_models())
            request = await fake.expect("model/list")
            fake.send_raw(b"this is not json\n")
            fake.send_raw(b"\n")  # a bare blank line is ignored, not counted
            await fake.drained()
            assert client.counters.malformed_frames == 1
            assert not task.done()
            fake.respond(request["id"], {"data": []})
            assert (await task).data == ()
        finally:
            await client.aclose()


async def test_a_burst_of_malformed_frames_fails_pending_requests() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.list_models())
            await fake.expect("model/list")
            for _ in range(protocol.MAX_CONSECUTIVE_MALFORMED_FRAMES):
                fake.send_raw(b"{not json\n")
            with pytest.raises(protocol.CodexFramingError):
                await task
            with pytest.raises(protocol.CodexProtocolError):
                await client.list_models()
        finally:
            await client.aclose()


async def test_semantically_malformed_frames_are_consecutive_and_fatal() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.list_models())
            await fake.expect("model/list")
            for _ in range(protocol.MAX_CONSECUTIVE_MALFORMED_FRAMES):
                fake.send({"id": "not-an-int", "result": {}})
            with pytest.raises(protocol.CodexFramingError):
                await task
            assert client.counters.malformed_frames == (
                protocol.MAX_CONSECUTIVE_MALFORMED_FRAMES
            )
        finally:
            await client.aclose()


async def test_a_non_object_frame_is_treated_as_malformed() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.send_raw(b"[1, 2, 3]\n")
            await fake.drained()
            assert client.counters.malformed_frames == 1
        finally:
            await client.aclose()


async def test_duplicate_response_ids_are_counted_as_orphans() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.list_models())
            request = await fake.expect("model/list")
            fake.respond(request["id"], {"data": []})
            assert (await task).data == ()
            fake.respond(request["id"], {"data": []})
            fake.send({"id": 4242, "result": {}})
            await fake.drained()
            assert client.counters.orphan_responses == 2
        finally:
            await client.aclose()


async def test_a_frame_carrying_both_result_and_error_is_malformed() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.list_models())
            request = await fake.expect("model/list")
            fake.send({"id": request["id"], "result": {}, "error": {"code": 1, "message": "x"}})
            await fake.drained()
            assert client.counters.malformed_frames == 1
            assert not task.done()
            fake.respond(request["id"], {"data": []})
            await task
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Timeout, cancellation, EOF
# ---------------------------------------------------------------------------


async def test_a_late_reply_to_a_timed_out_request_cannot_disturb_a_live_one() -> None:
    """The abandoned id — not some other already-completed id — must be discarded."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, request_timeout_s=0.05)
        try:
            with pytest.raises(protocol.CodexRequestTimeout):
                await client.list_models()
            abandoned_id = fake.frames_for("model/list")[0]["id"]
            # id 1 is `initialize`; the abandoned entry must be a *different* id,
            # or this test would only be proving that id 1 is already resolved.
            assert abandoned_id != 1
            assert client.pending_request_count == 0
            assert client.counters.orphan_responses == 0

            # A second request is in flight when the abandoned id's reply lands.
            second = asyncio.create_task(client.list_models())
            live = await fake.expect("model/list", index=1)
            assert live["id"] != abandoned_id
            fake.respond(abandoned_id, {"data": [], "nextCursor": "late-reply"})
            await fake.drained()
            assert client.counters.orphan_responses == 1
            assert not second.done()
            assert client.pending_request_count == 1

            fake.respond(live["id"], {"data": []})
            resolved = await second
            assert resolved.data == ()
            # Proves the live future took the live reply, not the late one.
            assert resolved.next_cursor is None
        finally:
            await client.aclose()


async def test_a_late_reply_to_a_cancelled_request_cannot_disturb_a_live_one() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.list_models())
            cancelled = await fake.expect("model/list")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.pending_request_count == 0

            second = asyncio.create_task(client.read_account())
            live = await fake.expect("account/read")
            fake.respond(cancelled["id"], {"data": [], "nextCursor": "late-reply"})
            await fake.drained()
            assert client.counters.orphan_responses == 1
            assert not second.done()

            fake.respond(live["id"], {"requiresOpenaiAuth": False})
            assert (await second).requires_openai_auth is False
        finally:
            await client.aclose()


async def test_eof_fails_every_pending_request_with_a_typed_error() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            first = asyncio.create_task(client.list_models())
            second = asyncio.create_task(client.read_account())
            await fake.expect("model/list")
            await fake.expect("account/read")
            fake.eof()
            with pytest.raises(protocol.CodexTransportClosed):
                await first
            with pytest.raises(protocol.CodexTransportClosed):
                await second
            assert client.pending_request_count == 0
        finally:
            await client.aclose()


async def test_calls_after_eof_fail_closed_instead_of_hanging() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.eof()
            await fake.drained()
            with pytest.raises(protocol.CodexTransportClosed):
                await client.list_models()
            with pytest.raises(protocol.CodexTransportClosed):
                await client.notify_initialized()
        finally:
            await client.aclose()


async def test_server_error_responses_raise_a_typed_redacted_error() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.read_account())
            request = await fake.expect("account/read")
            fake.respond_error(request["id"], -32000, f"token {JWT} was rejected")
            with pytest.raises(protocol.CodexRequestFailed) as caught:
                await task
            assert caught.value.code == -32000
            assert JWT not in str(caught.value)
            assert str(caught.value) == (
                "codex app-server request failed for account/read "
                "(code=-32000, reason=peer_error)"
            )
        finally:
            await client.aclose()


async def test_server_error_messages_never_reach_exceptions_or_reprs() -> None:
    """Peer messages are native content even when no token regex matches."""
    hostile = "account acct-plain prompt=delete-all cwd=/private/project output=done"
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.read_account())
            request = await fake.expect("account/read")
            fake.respond_error(request["id"], -32000, hostile)
            with pytest.raises(protocol.CodexRequestFailed) as caught:
                await task
            rendered = f"{caught.value!r} {caught.value}"
            for fragment in ("acct-plain", "delete-all", "/private/project", "done"):
                assert fragment not in rendered
            assert caught.value.method == "account/read"
            assert caught.value.code == -32000
        finally:
            await client.aclose()


async def test_an_unparseable_response_raises_a_schema_error() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            task = asyncio.create_task(client.read_account())
            request = await fake.expect("account/read")
            fake.respond(request["id"], {"requiresOpenaiAuth": "yes"})
            with pytest.raises(schema.CodexSchemaError):
                await task
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


async def test_supported_notifications_are_parsed_and_delivered() -> None:
    delivered: list[tuple[str, Any]] = []
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, notification_handler=lambda m, p: delivered.append((m, p)))
        try:
            fake.notify(
                "error",
                {
                    "error": {"message": "boom"},
                    "threadId": "t",
                    "turnId": "u",
                    "willRetry": True,
                },
            )
            fake.notify("account/updated", {"authMode": "chatgptAuthTokens"})
            await fake.drained()
            assert [method for method, _ in delivered] == ["error", "account/updated"]
            assert isinstance(delivered[0][1], schema.ErrorNotificationParams)
            assert isinstance(delivered[1][1], schema.AccountUpdatedParams)
        finally:
            await client.aclose()


async def test_unknown_notifications_are_counted_and_never_delivered() -> None:
    delivered: list[tuple[str, Any]] = []
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, notification_handler=lambda m, p: delivered.append((m, p)))
        try:
            fake.notify("thread/started", {"threadId": "t"})
            fake.notify("thread/started", {"threadId": "t"})
            fake.notify("totally/made/up", {"accessToken": JWT})
            await fake.drained()
            assert delivered == []
            counters = client.counters
            assert counters.unknown_notifications == 3
            assert counters.unknown_notification_methods == {
                "thread/started": 2,
                "<unrecognized>": 1,
            }
        finally:
            await client.aclose()


async def test_an_invalid_supported_notification_is_counted_not_raised() -> None:
    delivered: list[tuple[str, Any]] = []
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, notification_handler=lambda m, p: delivered.append((m, p)))
        try:
            fake.notify("error", {"threadId": "t"})
            fake.notify("account/updated", {"authMode": "chatgptAuthTokens"})
            await fake.drained()
            assert [method for method, _ in delivered] == ["account/updated"]
            assert client.counters.invalid_notifications == 1
        finally:
            await client.aclose()


async def test_a_failing_notification_handler_cannot_break_the_reader() -> None:
    def explode(method: str, params: Any) -> None:
        raise RuntimeError(f"handler blew up on {JWT}")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, notification_handler=explode)
        try:
            fake.notify("account/updated", {"authMode": "chatgpt"})
            await fake.drained()
            assert client.counters.notification_handler_errors == 1
            task = asyncio.create_task(client.list_models())
            request = await fake.expect("model/list")
            fake.respond(request["id"], {"data": []})
            await task
        finally:
            await client.aclose()


async def test_buffered_notifications_drop_the_oldest_under_queue_pressure() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, max_buffered_notifications=2)
        try:
            plans = ["free", "go", "plus", "pro", "enterprise"]
            for plan in plans:
                fake.notify("account/updated", {"planType": plan})
            await fake.drained()
            assert client.counters.dropped_notifications == 3
            kept: list[str | None] = []
            for _ in range(2):
                _, params = await client.next_notification(timeout=1.0)
                assert isinstance(params, schema.AccountUpdatedParams)
                kept.append(params.plan_type)
            assert kept == ["pro", "enterprise"]
            assert client.buffered_notification_count == 0
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Server-initiated requests — fail closed
# ---------------------------------------------------------------------------


async def _refresh(
    params: schema.ChatgptAuthTokensRefreshParams,
) -> schema.ChatgptAuthTokensRefreshResponse:
    return schema.ChatgptAuthTokensRefreshResponse(
        access_token=Redacted(JWT), chatgpt_account_id=Redacted(ACCOUNT_ID)
    )


async def test_the_refresh_server_request_is_answered_by_its_handler() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, refresh_handler=_refresh)
        try:
            fake.request(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized", "previousAccountId": None},
                request_id=90,
            )
            reply = await fake.receive_reply(90)
            assert reply["result"] == {
                "accessToken": JWT,
                "chatgptAccountId": ACCOUNT_ID,
            }
        finally:
            await client.aclose()


async def test_the_refresh_request_fails_closed_without_a_handler() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.request(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized"},
                request_id=91,
            )
            reply = await fake.receive_reply(91)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_METHOD_NOT_FOUND
        finally:
            await client.aclose()


async def test_invalid_refresh_params_return_invalid_params_not_a_refresh() -> None:
    calls: list[Any] = []

    async def handler(
        params: schema.ChatgptAuthTokensRefreshParams,
    ) -> schema.ChatgptAuthTokensRefreshResponse:
        calls.append(params)
        return await _refresh(params)

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, refresh_handler=handler)
        try:
            fake.request(
                "account/chatgptAuthTokens/refresh", {"reason": "expired"}, request_id=92
            )
            reply = await fake.receive_reply(92)
            assert reply["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
            assert calls == []
        finally:
            await client.aclose()


@pytest.mark.parametrize(
    "method",
    [
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
        "totally/made/up",
    ],
)
async def test_every_other_server_request_fails_closed(method: str) -> None:
    """No approval surface may be answered — least of all with a default allow."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, refresh_handler=_refresh)
        try:
            fake.request(method, {"decision": "approved"}, request_id=93)
            reply = await fake.receive_reply(93)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_METHOD_NOT_FOUND
            assert client.counters.unknown_server_requests == 1
        finally:
            await client.aclose()


async def test_a_failing_refresh_handler_returns_a_redacted_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(
        params: schema.ChatgptAuthTokensRefreshParams,
    ) -> schema.ChatgptAuthTokensRefreshResponse:
        raise RuntimeError(f"authority rejected {JWT}")

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, refresh_handler=handler)
        try:
            with caplog.at_level(logging.DEBUG, logger="server.codex.protocol"):
                fake.request(
                    "account/chatgptAuthTokens/refresh",
                    {"reason": "unauthorized"},
                    request_id=94,
                )
                reply = await fake.receive_reply(94)
            assert reply["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
            assert JWT not in json.dumps(reply)
            assert JWT not in _log_text(caplog)
        finally:
            await client.aclose()


async def test_a_server_request_with_a_non_integer_id_is_rejected() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, refresh_handler=_refresh)
        try:
            fake.send(
                {
                    "id": "not-an-int",
                    "method": "account/chatgptAuthTokens/refresh",
                    "params": {"reason": "unauthorized"},
                }
            )
            await fake.drained()
            assert client.counters.malformed_frames == 1
            assert fake.replies == {}
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


async def test_no_secret_shaped_value_reaches_the_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with FakeCodexAppServer() as fake:
        with caplog.at_level(logging.DEBUG, logger="server.codex.protocol"):
            client = await _ready(fake, refresh_handler=_refresh)
            try:
                login = asyncio.create_task(
                    client.login_with_external_tokens(
                        schema.ExternalAuthLoginParams(
                            access_token=Redacted(JWT),
                            chatgpt_account_id=Redacted(ACCOUNT_ID),
                        )
                    )
                )
                request = await fake.expect("account/login/start")
                fake.respond_error(request["id"], -32000, f"bad token {JWT}")
                with pytest.raises(protocol.CodexRequestFailed):
                    await login
                fake.notify("totally/made/up", {"accessToken": JWT, "prompt": "hello"})
                fake.request("item/tool/call", {"command": ["rm", "-rf", "/"]}, request_id=95)
                await fake.receive_reply(95)
                fake.eof()
                await fake.drained()
            finally:
                await client.aclose()
        text = _log_text(caplog)
        # Assert the paths under test actually logged: otherwise the negative
        # assertions below would pass just as well on a silent logger.
        events = {record.getMessage() for record in caplog.records}
        assert {
            "codex.protocol.initialized",
            "codex.protocol.unknown_notification",
            "codex.protocol.unknown_server_request",
        } <= events, events

        assert JWT not in text
        assert ACCOUNT_ID not in text
        # Server-request params are never logged at all; this pins that, so a
        # future change that starts logging them fails here rather than shipping.
        assert "rm -rf" not in text
        assert "-rf" not in text
        # And the one place a peer-controlled string *is* logged is scrubbed.
        assert REDACTED in redaction.scrub_text(f"bad token {JWT}")


async def test_client_repr_never_exposes_transport_material() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            assert JWT not in repr(client)
            assert "CodexAppServerClient" in repr(client)
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Bounded server-request path and the external-auth gate (review round 1)
# ---------------------------------------------------------------------------


async def test_inbound_server_requests_are_bounded_and_shed_without_answering() -> None:
    """A flood of approval requests must not grow tasks or the journal."""
    limit = 4
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, max_inflight_server_requests=limit)
        try:
            fake.transport.stall_writes(True)
            for request_id in range(200, 400):
                fake.request("execCommandApproval", {"decision": "approved"}, request_id=request_id)
            await fake.drained()

            assert client.inflight_server_request_count <= limit
            counters = client.counters
            assert counters.unknown_server_requests == 200
            assert counters.shed_server_requests == 200 - limit
            # Nothing was answered, and above all nothing was approved.
            assert fake.replies == {}
        finally:
            fake.transport.stall_writes(False)
            await client.aclose()


async def test_a_server_request_flood_logs_once_per_method_not_once_per_frame(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with FakeCodexAppServer() as fake:
        with caplog.at_level(logging.DEBUG, logger="server.codex.protocol"):
            client = await _ready(fake, max_inflight_server_requests=2)
            try:
                fake.transport.stall_writes(True)
                for request_id in range(500, 600):
                    fake.request("execCommandApproval", {}, request_id=request_id)
                for request_id in range(600, 700):
                    fake.request("applyPatchApproval", {}, request_id=request_id)
                await fake.drained()
            finally:
                fake.transport.stall_writes(False)
                await client.aclose()
        messages = [r.getMessage() for r in caplog.records]
        assert messages.count("codex.protocol.unknown_server_request") == 2
        assert messages.count("codex.protocol.server_requests_saturated") == 1


async def test_external_token_login_is_refused_without_the_experimental_api() -> None:
    """A client that advertised experimentalApi=false must not ship a token."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=False)
        try:
            assert fake.client_frames[0]["params"]["capabilities"]["experimentalApi"] is False
            with pytest.raises(protocol.CodexExternalAuthDisabled):
                await client.login_with_external_tokens(
                    schema.ExternalAuthLoginParams(
                        access_token=Redacted(JWT),
                        chatgpt_account_id=Redacted(ACCOUNT_ID),
                    )
                )
            assert fake.frames_for("account/login/start") == []
            assert JWT not in json.dumps(fake.client_frames)
        finally:
            await client.aclose()


async def test_a_second_initialize_is_refused() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            with pytest.raises(protocol.CodexProtocolError):
                await client.initialize()
            assert len(fake.frames_for("initialize")) == 1
        finally:
            await client.aclose()


async def test_a_refresh_handler_returning_a_non_dto_never_reaches_the_wire() -> None:
    """The handler is external code; an arbitrary dict must not be relayed."""

    async def rogue(params: schema.ChatgptAuthTokensRefreshParams) -> Any:
        return {"accessToken": JWT, "chatgptAccountId": ACCOUNT_ID, "extra": "junk"}

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, refresh_handler=rogue)
        try:
            fake.request(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized"},
                request_id=96,
            )
            reply = await fake.receive_reply(96)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
            assert JWT not in json.dumps(reply)
        finally:
            await client.aclose()


async def test_the_unhandled_server_request_counter_tracks_a_missing_authority() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            for request_id in (97, 98):
                fake.request(
                    "account/chatgptAuthTokens/refresh",
                    {"reason": "unauthorized"},
                    request_id=request_id,
                )
                await fake.receive_reply(request_id)
            counters = client.counters
            assert counters.unhandled_server_requests == 2
            # A supported-but-unserviceable method is not an *unknown* method.
            assert counters.unknown_server_requests == 0
        finally:
            await client.aclose()


async def test_repeated_calls_on_a_closed_client_do_not_grow_one_traceback() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            fake.eof()
            await fake.drained()
            depths: list[int] = []
            for _ in range(5):
                with pytest.raises(protocol.CodexTransportClosed) as caught:
                    await client.list_models()
                depth = 0
                tb = caught.value.__traceback__
                while tb is not None:
                    depth += 1
                    tb = tb.tb_next
                depths.append(depth)
            assert len(set(depths)) == 1, depths
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# External-auth gating in both directions (review round 2)
# ---------------------------------------------------------------------------


def _assert_no_credentials_on_the_wire(fake: FakeCodexAppServer) -> None:
    """No frame the client wrote may carry a token- or JWT-shaped value."""
    for frame in fake.client_frames:
        rendered = json.dumps(frame)
        assert JWT not in rendered, frame.get("method") or frame.get("id")
        assert not redaction.contains_secret_shaped_value(rendered), frame


async def test_a_refresh_request_is_unsupported_without_the_experimental_api() -> None:
    """The default config installs a handler but disables external auth."""
    calls: list[schema.ChatgptAuthTokensRefreshParams] = []

    async def handler(
        params: schema.ChatgptAuthTokensRefreshParams,
    ) -> schema.ChatgptAuthTokensRefreshResponse:
        calls.append(params)
        return await _refresh(params)

    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=False, refresh_handler=handler)
        try:
            assert fake.client_frames[0]["params"]["capabilities"]["experimentalApi"] is False
            fake.request(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized"},
                request_id=77,
            )
            reply = await fake.receive_reply(77)
            assert "result" not in reply
            assert reply["error"]["code"] == protocol.JSONRPC_METHOD_NOT_FOUND
            # Reported unsupported, not merely unhandled: one fail-closed path.
            assert client.counters.unknown_server_requests == 1
            assert client.counters.unhandled_server_requests == 0
            assert calls == []
            _assert_no_credentials_on_the_wire(fake)
        finally:
            await client.aclose()


async def test_a_refresh_request_is_still_answered_when_external_auth_is_on() -> None:
    """The gate must not break the configuration the feature actually needs."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=True, refresh_handler=_refresh)
        try:
            fake.request(
                "account/chatgptAuthTokens/refresh",
                {"reason": "unauthorized"},
                request_id=78,
            )
            reply = await fake.receive_reply(78)
            assert reply["result"]["accessToken"] == JWT
            assert client.counters.unknown_server_requests == 0
        finally:
            await client.aclose()


async def test_the_raw_call_door_is_gated_for_external_token_methods() -> None:
    """`login_with_external_tokens` must not be the only gated door."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=False)
        try:
            params = schema.ExternalAuthLoginParams(
                access_token=Redacted(JWT), chatgpt_account_id=Redacted(ACCOUNT_ID)
            )
            with pytest.raises(protocol.CodexExternalAuthDisabled):
                await client.call(schema.METHOD_LOGIN_START, params.to_wire())
            with pytest.raises(protocol.CodexExternalAuthDisabled):
                await client.login_with_external_tokens(params)
            assert fake.frames_for("account/login/start") == []
            assert ACCOUNT_ID not in json.dumps(fake.client_frames)
            _assert_no_credentials_on_the_wire(fake)
        finally:
            await client.aclose()


async def test_the_raw_call_door_still_works_when_external_auth_is_on() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, external_auth_enabled=True)
        try:
            fake.rule("account/login/start", result={"type": "chatgptAuthTokens"})
            fake.start_autopilot()
            params = schema.ExternalAuthLoginParams(
                access_token=Redacted(JWT), chatgpt_account_id=Redacted(ACCOUNT_ID)
            )
            await client.call(schema.METHOD_LOGIN_START, params.to_wire())
            assert fake.frames_for("account/login/start")[0]["params"]["accessToken"] == JWT
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Write deadlines (review round 2)
# ---------------------------------------------------------------------------


async def test_a_stalled_transport_cannot_outlive_the_request_deadline() -> None:
    """The outbound write is inside the deadline, not before it."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, request_timeout_s=0.2)
        try:
            fake.transport.stall_writes(True)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(protocol.CodexRequestTimeout):
                await asyncio.wait_for(client.list_models(), 2.0)
            elapsed = loop.time() - started
            assert elapsed < 1.5, elapsed
            assert client.pending_request_count == 0
        finally:
            fake.transport.stall_writes(False)
            await client.aclose()


async def test_outbound_requests_fail_fast_at_the_configured_limit() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, max_outbound_requests=2)
        try:
            fake.transport.stall_writes(True)
            first = asyncio.create_task(client.list_models())
            second = asyncio.create_task(client.read_account())
            for _ in range(100):
                if client.pending_request_count == 2:
                    break
                await asyncio.sleep(0)
            assert client.pending_request_count == 2

            with pytest.raises(protocol.CodexRequestOverloaded):
                await asyncio.wait_for(client.read_rate_limits(), 0.1)
            assert client.pending_request_count == 2
            assert client.counters.rejected_outbound_requests == 1

            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert client.pending_request_count == 1

            replacement = asyncio.create_task(client.list_models())
            for _ in range(100):
                if client.pending_request_count == 2:
                    break
                await asyncio.sleep(0)
            assert client.pending_request_count == 2

            second.cancel()
            replacement.cancel()
            await asyncio.gather(second, replacement, return_exceptions=True)
            assert client.pending_request_count == 0
        finally:
            fake.transport.stall_writes(False)
            await client.aclose()


async def test_outbound_burst_is_admitted_before_internal_task_creation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, max_outbound_requests=2, request_timeout_s=60.0)
        fake.transport.stall_writes(True)
        with caplog.at_level(logging.WARNING, logger="server.codex.protocol"):
            callers = [asyncio.create_task(client.list_models()) for _ in range(1000)]
            # Let every application caller reach admission. With admission in
            # the internal coroutine, all 1,000 internal tasks exist here.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        try:
            assert client.active_outbound_request_count <= 2
            assert client.pending_request_count <= 2
            assert client.counters.rejected_outbound_requests == 998
            assert [record.getMessage() for record in caplog.records].count(
                "codex.protocol.outbound_requests_saturated"
            ) == 1
        finally:
            for caller in callers:
                if not caller.done():
                    caller.cancel()
            await asyncio.gather(*callers, return_exceptions=True)
            fake.transport.stall_writes(False)
            await client.aclose()


async def test_outbound_timeout_releases_capacity_for_the_next_request() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(
            fake, max_outbound_requests=1, request_timeout_s=0.05
        )
        try:
            with pytest.raises(protocol.CodexRequestTimeout):
                await client.list_models()
            assert client.pending_request_count == 0

            second = asyncio.create_task(client.read_account())
            request = await fake.expect("account/read")
            fake.respond(request["id"], {"requiresOpenaiAuth": False})
            assert (await second).requires_openai_auth is False
            assert client.counters.rejected_outbound_requests == 0
        finally:
            await client.aclose()


async def test_aclose_cancels_and_reaps_requests_stalled_in_write() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, max_outbound_requests=2, request_timeout_s=60.0)
        fake.transport.stall_writes(True)
        first = asyncio.create_task(client.list_models())
        second = asyncio.create_task(client.read_account())
        for _ in range(100):
            if client.pending_request_count == 2:
                break
            await asyncio.sleep(0)
        assert client.pending_request_count == 2

        await asyncio.wait_for(client.aclose(), 0.5)
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, protocol.CodexTransportClosed) for result in results)
        assert client.pending_request_count == 0
        assert client.active_outbound_request_count == 0


async def test_aclose_never_awaits_a_cancellation_resistant_caller() -> None:
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, request_timeout_s=60.0)
        fake.transport.stall_writes(True)
        caller_was_cancelled = asyncio.Event()
        keep_caller_alive = asyncio.Event()

        async def resistant_caller() -> Any:
            try:
                return await client.list_models()
            except asyncio.CancelledError:
                caller_was_cancelled.set()
                await keep_caller_alive.wait()
                raise

        caller = asyncio.create_task(resistant_caller())
        for _ in range(100):
            if client.pending_request_count == 1:
                break
            await asyncio.sleep(0)
        assert client.pending_request_count == 1

        try:
            await asyncio.wait_for(client.aclose(), 0.5)
            assert not caller_was_cancelled.is_set()
            result = (await asyncio.gather(caller, return_exceptions=True))[0]
            assert isinstance(result, protocol.CodexTransportClosed)
        finally:
            keep_caller_alive.set()
            if not caller.done():
                caller.cancel()
                await asyncio.gather(caller, return_exceptions=True)


async def test_aclose_cancels_initialize_stalled_writing_initialized() -> None:
    async with FakeCodexAppServer() as fake:
        client = _client(fake, request_timeout_s=60.0)
        await client.start()
        initializing = asyncio.create_task(client.initialize())
        request = await fake.receive()
        fake.transport.stall_writes(True)
        fake.respond(request["id"], fake.default_initialize_response())
        for _ in range(100):
            if (
                client.pending_request_count == 0
                and client.active_outbound_request_count == 1
                and not initializing.done()
            ):
                break
            await asyncio.sleep(0)
        assert client.active_outbound_request_count == 1

        try:
            await asyncio.wait_for(client.aclose(), 0.5)
            done, _ = await asyncio.wait({initializing}, timeout=0.1)
            assert done == {initializing}
            result = (await asyncio.gather(initializing, return_exceptions=True))[0]
            assert isinstance(result, protocol.CodexTransportClosed)
            assert client.active_outbound_request_count == 0
        finally:
            if not initializing.done():
                initializing.cancel()
                await asyncio.gather(initializing, return_exceptions=True)


async def test_a_reply_blocked_on_a_stalled_transport_cannot_starve_a_call() -> None:
    """A stuck reply task holds the write lock; the deadline must still fire."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake, request_timeout_s=0.2)
        try:
            fake.transport.stall_writes(True)
            fake.request("execCommandApproval", {}, request_id=79)
            await fake.drained()
            assert client.inflight_server_request_count == 1

            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(protocol.CodexRequestTimeout):
                await asyncio.wait_for(client.list_models(), 2.0)
            assert loop.time() - started < 1.5

            # The stuck reply task is itself bounded and eventually released.
            for _ in range(100):
                if client.inflight_server_request_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert client.inflight_server_request_count == 0
            assert fake.replies == {}
        finally:
            fake.transport.stall_writes(False)
            await client.aclose()


async def test_closed_copy_survives_a_subclass_with_a_richer_init() -> None:
    """`_fail_all` uses this; a raise here would strand every pending caller."""
    async with FakeCodexAppServer() as fake:
        client = await _ready(fake)
        try:
            client._fail_all(  # noqa: SLF001 -- exercising the latch directly
                protocol.CodexRequestFailed(-32000, "boom", method="account/read")
            )
            for _ in range(3):
                with pytest.raises(protocol.CodexProtocolError):
                    await client.list_models()
        finally:
            await client.aclose()
