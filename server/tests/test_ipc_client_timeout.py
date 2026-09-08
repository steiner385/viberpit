"""A wedged or mid-response-reset daemon RPC must surface as a typed DaemonUnavailable,
never a raw httpx exception bubbling to an unhandled 500."""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx

import pytest

from server.ipc.client import DaemonClient, DaemonTimeout, DaemonUnavailable
from server.ipc.daemon import create_daemon_app, register_rpc
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_call_timeout_raises_daemon_unavailable():
    app = create_daemon_app()

    async def slow(params):
        await asyncio.sleep(5)
        return {}

    register_rpc(app, "slow", slow)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            with pytest.raises(DaemonUnavailable):
                await client.call("slow", timeout=0.2)
        finally:
            await client.aclose()


# --------------------------------------------------------------------------- #
# outcome_unknown: did the daemon possibly RUN the RPC?
#
# A read timeout or a mid-response reset means the request reached the daemon and
# may have completed — a `create_session` that times out at the edge still finishes
# on the daemon (2026-09-07: edge 503'd at 30s, the session row landed 2s later, the
# user's retry made a duplicate). A pool timeout or connect failure means the daemon
# was never asked. Callers that would otherwise retry or fail over (the fleet's
# next-host cascade) need to tell the two apart, so the client stamps the verdict on
# the exception rather than every caller re-deriving it from httpx internals.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_read_timeout_marks_outcome_unknown():
    app = create_daemon_app()

    async def slow(params):
        await asyncio.sleep(5)
        return {}

    register_rpc(app, "slow", slow)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            with pytest.raises(DaemonTimeout) as ei:
                await client.call("slow", timeout=0.2)
            assert ei.value.outcome_unknown is True
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_read_error_marks_outcome_unknown():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            with patch.object(
                client._http, "post",
                AsyncMock(side_effect=httpx.ReadError("connection reset by peer")),
            ):
                with pytest.raises(DaemonUnavailable) as ei:
                    await client.call("ping")
            assert ei.value.outcome_unknown is True
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_never_dispatched_failures_leave_outcome_known():
    """ConnectError and PoolTimeout fail on OUR side of the wire — the daemon was
    never asked, so retrying or failing over cannot duplicate anything."""
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            with patch.object(
                client._http, "post",
                AsyncMock(side_effect=httpx.ConnectError("connection refused")),
            ):
                with pytest.raises(DaemonUnavailable) as ei:
                    await client.call("ping")
            assert ei.value.outcome_unknown is False

            with patch.object(
                client._http, "post",
                AsyncMock(side_effect=httpx.PoolTimeout("no slot")),
            ):
                with pytest.raises(DaemonTimeout) as ei:
                    await client.call("ping")
            assert ei.value.outcome_unknown is False
        finally:
            await client.aclose()


def test_outcome_unknown_defaults_to_false():
    """Every existing raise site (and test double) constructs with a bare message."""
    assert DaemonUnavailable("x").outcome_unknown is False
    assert DaemonTimeout("x").outcome_unknown is False


@pytest.mark.asyncio
async def test_call_with_explicit_timeout_still_succeeds_fast():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            assert await client.call("ping", timeout=5.0) == {"pong": True}
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_call_read_error_raises_daemon_unavailable():
    """A ReadError (daemon closed connection mid-response) must surface as
    DaemonUnavailable — not a raw httpx.ReadError causing an unhandled 500
    (the scenario that hit /api/sessions on 2026-07-10)."""
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            with patch.object(
                client._http, "post",
                AsyncMock(side_effect=httpx.ReadError("connection reset by peer")),
            ):
                with pytest.raises(DaemonUnavailable):
                    await client.call("ping")
        finally:
            await client.aclose()
