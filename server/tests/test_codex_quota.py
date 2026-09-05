"""Codex quota: bucket mapping, the four states, and the last-good cache.

Payloads are wire dicts parsed by the real `schema.parse_rate_limits_response`,
so a drift in the pinned `account/rateLimits/read` contract fails here instead of
being papered over by a hand-built DTO.
"""
from __future__ import annotations

from typing import Any

import pytest

from server.codex import protocol, schema
from server.codex.quota import (
    STALE_CEILING_MULTIPLIER,
    CodexQuotaReader,
    QuotaSnapshot,
    QuotaState,
    QuotaWindow,
    snapshot_from_rate_limits,
)

NOW = 1_800_000_000.0


def _window(
    used: int, *, resets_at: int | None = None, minutes: int | None = None
) -> dict[str, Any]:
    wire: dict[str, Any] = {"usedPercent": used}
    if resets_at is not None:
        wire["resetsAt"] = resets_at
    if minutes is not None:
        wire["windowDurationMins"] = minutes
    return wire


def _response(
    *,
    primary: dict[str, Any] | None = None,
    secondary: dict[str, Any] | None = None,
    plan_type: str | None = "pro",
    credits: dict[str, Any] | None = None,
    reached: str | None = None,
    by_limit_id: dict[str, dict[str, Any]] | None = None,
) -> schema.GetAccountRateLimitsResponse:
    default_primary = _window(42, resets_at=int(NOW) + 3600, minutes=300)
    limits: dict[str, Any] = {
        "primary": default_primary if primary is None else primary,
        "secondary": secondary,
        "planType": plan_type,
        "credits": credits,
        "rateLimitReachedType": reached,
    }
    payload: dict[str, Any] = {"accountId": "acct-0123456789", "rateLimits": limits}
    if by_limit_id is not None:
        payload["rateLimitsByLimitId"] = by_limit_id
    return schema.parse_rate_limits_response(payload)


class FakeRateLimitClient:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0

    async def read_rate_limits(self) -> schema.GetAccountRateLimitsResponse:
        self.calls += 1
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Bucket mapping
# ---------------------------------------------------------------------------


def test_maps_primary_and_secondary_windows() -> None:
    response = _response(
        primary=_window(42, resets_at=int(NOW) + 3600, minutes=300),
        secondary=_window(88, resets_at=int(NOW) + 86_400, minutes=10_080),
    )

    snapshot = snapshot_from_rate_limits(response, now=NOW)

    assert snapshot.state is QuotaState.OK
    assert snapshot.windows == (
        QuotaWindow(
            id="primary",
            label="Primary",
            used_percent=42,
            resets_at=NOW + 3600,
            window_minutes=300,
        ),
        QuotaWindow(
            id="secondary",
            label="Secondary",
            used_percent=88,
            resets_at=NOW + 86_400,
            window_minutes=10_080,
        ),
    )
    assert snapshot.age_s == 0.0


def test_a_missing_reset_stays_none() -> None:
    """`None` must survive: `0` renders as 1970 and "now" renders as
    "resets immediately"."""
    response = _response(primary=_window(0), secondary=None)

    (window,) = snapshot_from_rate_limits(response, now=NOW).windows

    assert window.resets_at is None
    assert window.window_minutes is None
    assert window.used_percent == 0


def test_absent_buckets_are_absent_not_fabricated() -> None:
    response = schema.parse_rate_limits_response({"rateLimits": {}})

    snapshot = snapshot_from_rate_limits(response, now=NOW)

    assert snapshot.windows == ()
    assert snapshot.state is QuotaState.OK


def test_model_scoped_buckets_carry_the_limit_id() -> None:
    response = _response(
        by_limit_id={
            "gpt-5-codex": {
                "primary": _window(10, resets_at=int(NOW) + 60, minutes=300),
                "secondary": _window(20),
                "limitId": "gpt-5-codex",
                "limitName": "GPT-5 Codex",
            }
        }
    )

    snapshot = snapshot_from_rate_limits(response, now=NOW)

    scoped = [window for window in snapshot.windows if window.model is not None]
    assert [window.id for window in scoped] == [
        "gpt-5-codex:primary",
        "gpt-5-codex:secondary",
    ]
    assert [window.label for window in scoped] == [
        "GPT-5 Codex (primary)",
        "GPT-5 Codex (secondary)",
    ]
    assert {window.model for window in scoped} == {"gpt-5-codex"}


def test_a_model_scoped_bucket_without_a_name_is_labelled_by_id() -> None:
    response = _response(by_limit_id={"o4-mini": {"primary": _window(5)}})

    scoped = [
        window
        for window in snapshot_from_rate_limits(response, now=NOW).windows
        if window.model == "o4-mini"
    ]

    assert [window.label for window in scoped] == ["o4-mini (primary)"]


def test_plan_credits_and_reached_type_are_carried() -> None:
    response = _response(
        plan_type="pro",
        credits={"hasCredits": True, "unlimited": False, "balance": "12.50"},
        reached="rate_limit_reached",
    )

    snapshot = snapshot_from_rate_limits(response, now=NOW)

    assert snapshot.plan_type == "pro"
    assert snapshot.has_credits is True
    assert snapshot.credits_unlimited is False
    assert snapshot.rate_limit_reached == "rate_limit_reached"


def test_an_elapsed_reset_makes_the_snapshot_report_its_own_staleness() -> None:
    response = _response(primary=_window(70, resets_at=int(NOW) - 600, minutes=300))

    snapshot = snapshot_from_rate_limits(response, now=NOW)

    assert snapshot.age_s == pytest.approx(600.0)


def test_wire_shape_never_carries_the_native_account_id() -> None:
    response = _response(
        credits={"hasCredits": False, "unlimited": True, "balance": None}
    )

    wire = snapshot_from_rate_limits(response, now=NOW).to_wire()

    assert wire["state"] == "ok"
    assert wire["planType"] == "pro"
    assert wire["creditsUnlimited"] is True
    assert wire["ageS"] == 0.0
    assert wire["windows"][0]["usedPercent"] == 42
    assert "acct-0123456789" not in str(wire)


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_read_is_ok_and_fresh() -> None:
    client = FakeRateLimitClient(_response())
    reader = CodexQuotaReader(clock=FakeClock())

    snapshot = await reader.read("acct", client=client)

    assert snapshot.state is QuotaState.OK
    assert snapshot.age_s == 0.0
    assert snapshot.detail == ""


@pytest.mark.asyncio
async def test_method_not_found_is_unsupported() -> None:
    client = FakeRateLimitClient(
        protocol.CodexRequestFailed(
            protocol.JSONRPC_METHOD_NOT_FOUND,
            "unknown method",
            method="account/rateLimits/read",
        )
    )
    reader = CodexQuotaReader(clock=FakeClock())

    snapshot = await reader.read("acct", client=client)

    assert snapshot.state is QuotaState.UNSUPPORTED
    assert snapshot.windows == ()
    assert snapshot.age_s is None


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.asyncio
async def test_auth_shaped_failures_are_auth_error(code: int) -> None:
    client = FakeRateLimitClient(
        protocol.CodexRequestFailed(code, "unauthorized", method="x")
    )
    reader = CodexQuotaReader(clock=FakeClock())

    snapshot = await reader.read("acct", client=client)

    assert snapshot.state is QuotaState.AUTH_ERROR


@pytest.mark.parametrize(
    "error",
    [
        protocol.CodexRequestFailed(-32603, "internal error", method="x"),
        protocol.CodexRequestTimeout("deadline exceeded"),
        protocol.CodexTransportClosed("stream ended"),
        schema.CodexSchemaError("field 'rateLimits' is required"),
    ],
)
@pytest.mark.asyncio
async def test_everything_else_is_unknown(error: BaseException) -> None:
    reader = CodexQuotaReader(clock=FakeClock())

    snapshot = await reader.read("acct", client=FakeRateLimitClient(error))

    assert snapshot.state is QuotaState.UNKNOWN
    assert snapshot.detail


@pytest.mark.asyncio
async def test_missing_client_is_unknown_not_an_exception() -> None:
    reader = CodexQuotaReader(clock=FakeClock())

    snapshot = await reader.read("acct", client=None)

    assert snapshot == QuotaSnapshot(
        state=QuotaState.UNKNOWN,
        detail="no codex app-server connection is available for this account",
    )


@pytest.mark.asyncio
async def test_failure_detail_is_scrubbed() -> None:
    client = FakeRateLimitClient(
        protocol.CodexRequestFailed(-32603, "token sk-abcdefghijklmnop rejected")
    )
    reader = CodexQuotaReader(clock=FakeClock())

    snapshot = await reader.read("acct", client=client)

    assert "sk-abcdefghijklmnop" not in snapshot.detail
    # Since #1172 the detail is a fixed category, not a redacted message — a
    # stronger guarantee, and the one the quota surface renders to operators.
    assert snapshot.detail == "runtime_error"


# ---------------------------------------------------------------------------
# Last-good cache and its staleness ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_good_is_served_with_its_age_when_a_read_fails() -> None:
    clock = FakeClock()
    client = FakeRateLimitClient(
        _response(), protocol.CodexTransportClosed("stream ended")
    )
    reader = CodexQuotaReader(cache_ttl_s=60.0, clock=clock)

    await reader.read("acct", client=client)
    clock.now += 90.0
    snapshot = await reader.read("acct", client=client)

    assert snapshot.state is QuotaState.OK
    assert snapshot.age_s == pytest.approx(90.0)
    assert snapshot.windows[0].used_percent == 42


@pytest.mark.asyncio
async def test_last_good_expires_at_the_staleness_ceiling() -> None:
    clock = FakeClock()
    client = FakeRateLimitClient(
        _response(), protocol.CodexTransportClosed("stream ended")
    )
    reader = CodexQuotaReader(cache_ttl_s=60.0, clock=clock)

    await reader.read("acct", client=client)
    clock.now += 60.0 * STALE_CEILING_MULTIPLIER + 1.0
    snapshot = await reader.read("acct", client=client)

    assert snapshot.state is QuotaState.UNKNOWN
    assert snapshot.windows == ()
    assert snapshot.age_s == pytest.approx(601.0)


@pytest.mark.asyncio
async def test_last_good_is_per_account() -> None:
    clock = FakeClock()
    reader = CodexQuotaReader(cache_ttl_s=60.0, clock=clock)

    await reader.read("acct", client=FakeRateLimitClient(_response()))
    snapshot = await reader.read(
        "other", client=FakeRateLimitClient(protocol.CodexTransportClosed("gone"))
    )

    assert snapshot.state is QuotaState.UNKNOWN
    assert snapshot.age_s is None


@pytest.mark.asyncio
async def test_cached_returns_the_last_good_read_within_the_ttl() -> None:
    clock = FakeClock()
    reader = CodexQuotaReader(cache_ttl_s=60.0, clock=clock)

    assert reader.cached("acct") is None
    await reader.read("acct", client=FakeRateLimitClient(_response()))
    clock.now += 30.0
    cached = reader.cached("acct")

    assert cached is not None
    assert cached.state is QuotaState.OK
    assert cached.age_s == pytest.approx(30.0)

    clock.now += 31.0
    assert reader.cached("acct") is None


@pytest.mark.asyncio
async def test_invalidate_drops_one_account_then_all_of_them() -> None:
    reader = CodexQuotaReader(cache_ttl_s=60.0, clock=FakeClock())

    await reader.read("acct", client=FakeRateLimitClient(_response()))
    await reader.read("other", client=FakeRateLimitClient(_response()))

    reader.invalidate("acct")
    assert reader.cached("acct") is None
    assert reader.cached("other") is not None

    reader.invalidate()
    assert reader.cached("other") is None
