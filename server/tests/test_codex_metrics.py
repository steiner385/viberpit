"""Codex boundary metrics (plan Task 6.1) and the labelled primitives they need.

Two things are tested here and they are different in kind:

* the **registry arithmetic** for the label support this change added to
  ``Histogram`` and ``Gauge`` (``Counter`` already had it), including the
  guard-rail errors — the primitives are shared with the assistant series, so a
  regression here is not confined to Codex; and
* the **wiring** at each real call site, so a series cannot quietly stop being
  fed. Every wiring assertion is a DELTA around the call under test: these are
  process-wide module singletons shared with the whole suite, exactly as
  ``test_assistant_metrics.py`` notes.

One property gets its own test because it is a privacy contract rather than a
behaviour: **no native Codex identifier is ever a label value.** The plan says to
hash native ids; the label sets here never collect one, which is stronger, and
``test_no_native_identifier_is_used_as_a_label`` is what keeps it that way.
"""
from __future__ import annotations

import asyncio

import pytest

from server.obs import metrics as m


# ---------------------------------------------------------------------------
# The labelled primitives
# ---------------------------------------------------------------------------


def test_labelled_histogram_tracks_count_and_sum_per_label_tuple() -> None:
    hist = m.Histogram("t_hist", "help", labelnames=("method", "outcome"))
    hist.labels("thread/start", "ok").observe(10.0)
    hist.labels("thread/start", "ok").observe(30.0)
    hist.labels("thread/start", "error").observe(5.0)

    assert hist.count_for("thread/start", "ok") == 2
    assert hist.sum_for("thread/start", "ok") == 40.0
    assert hist.count_for("thread/start", "error") == 1
    # An unobserved combination reads zero rather than raising.
    assert hist.count_for("turn/start", "ok") == 0
    assert hist.sum_for("turn/start", "ok") == 0.0


def test_unlabelled_histogram_still_works_for_the_assistant_series() -> None:
    hist = m.Histogram("t_plain", "help")
    hist.observe(2.0)
    hist.observe(3.0)
    assert hist.count == 2
    assert hist.sum == 5.0


def test_histogram_refuses_to_mix_labelled_and_unlabelled_use() -> None:
    labelled = m.Histogram("t_h2", "help", labelnames=("a",))
    with pytest.raises(TypeError):
        labelled.observe(1.0)
    with pytest.raises(TypeError):
        labelled.labels("a", "b")


def test_labelled_gauge_moves_up_down_and_sets_per_label_tuple() -> None:
    gauge = m.Gauge("t_gauge", "help", labelnames=("account",))
    gauge.labels("alpha").inc()
    gauge.labels("alpha").inc()
    gauge.labels("alpha").dec()
    gauge.labels("beta").set(7.0)

    assert gauge.value_for("alpha") == 1.0
    assert gauge.value_for("beta") == 7.0
    assert gauge.value_for("gamma") == 0.0


def test_unlabelled_gauge_gains_set_without_losing_inc_dec() -> None:
    gauge = m.Gauge("t_g2", "help")
    gauge.inc(3.0)
    gauge.dec()
    assert gauge.value == 2.0
    gauge.set(9.0)
    assert gauge.value == 9.0


def test_gauge_refuses_to_mix_labelled_and_unlabelled_use() -> None:
    labelled = m.Gauge("t_g3", "help", labelnames=("a",))
    with pytest.raises(TypeError):
        labelled.inc()
    with pytest.raises(TypeError):
        labelled.set(1.0)
    with pytest.raises(TypeError):
        labelled.labels()


# ---------------------------------------------------------------------------
# The privacy contract on the label sets
# ---------------------------------------------------------------------------


CODEX_SERIES = (
    m.codex_build_info,
    m.codex_request_ms,
    m.codex_threads_active,
    m.codex_turns_active,
    m.codex_runner_restarts_total,
    m.codex_auth_requests_total,
    m.codex_auth_request_ms,
    m.codex_auth_generation,
    m.codex_quota_last_read_timestamp_s,
    m.codex_approval_ms,
)

#: Label names that would carry a native Codex identifier or user content. A
#: series gaining one of these is the regression this test exists to catch —
#: metrics are retained far longer than logs and are not covered by the
#: audit-log redactor.
FORBIDDEN_LABELS = frozenset(
    {
        "thread", "thread_id", "threadid",
        "turn", "turn_id", "turnid",
        "conversation", "conversation_id", "rollout", "rollout_id",
        "session", "session_id",
        "prompt", "command", "path", "cwd", "content", "message",
        "token", "access_token", "credential",
        "chatgpt_account_id", "user", "email",
    }
)


def test_no_native_identifier_is_used_as_a_label() -> None:
    for series in CODEX_SERIES:
        offending = set(series.labelnames) & FORBIDDEN_LABELS
        assert not offending, f"{series.name} labels on {sorted(offending)}"


def test_every_codex_series_is_labelled_with_a_closed_vocabulary() -> None:
    """`account` is the only unbounded-looking label, and it is bounded by the
    account manifest — everything else is a method name, an outcome, or the
    process-constant build identity."""
    allowed = {"account", "method", "outcome", "engine", "provider", "host", "cli_version"}
    for series in CODEX_SERIES:
        assert set(series.labelnames) <= allowed, series.name


# ---------------------------------------------------------------------------
# Wiring: protocol request latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_app_server_request_is_timed_including_failures() -> None:
    """`CodexAppServerClient.call` is the one door every request goes through, so
    the histogram is fed there rather than per wrapper.

    `thread/archive` is the method under test only because its response parses
    from any object — this exercises `call`'s own timing, not a payload shape.
    """
    from server.codex import protocol, schema

    method = schema.METHOD_THREAD_ARCHIVE
    params = {"threadId": "th_metrics"}
    before_ok = m.codex_request_ms.count_for(method, "ok")
    before_err = m.codex_request_ms.count_for(method, "error")

    class _Client(protocol.CodexAppServerClient):
        """Only the transport half is faked; `call`'s own body is under test."""

        def __init__(self, outcome: str) -> None:
            super().__init__(_NullTransport(), external_auth_enabled=True)
            self._initialized = True
            self._outcome = outcome

        async def _run_owned_operation(self, operation, *, method):  # type: ignore[override]
            if self._outcome == "error":
                raise protocol.CodexProtocolError("boom")
            return {}

    await _Client("ok").call(method, params)
    with pytest.raises(protocol.CodexProtocolError):
        await _Client("error").call(method, params)

    assert m.codex_request_ms.count_for(method, "ok") == before_ok + 1
    assert m.codex_request_ms.count_for(method, "error") == before_err + 1


class _NullTransport:
    """An `AppServerTransport` that is never driven — the test overrides
    `_run_owned_operation`, so no frame is ever read or written."""

    async def read_line(self) -> bytes:  # pragma: no cover - never called
        raise AssertionError("the metrics test never drives the transport")

    async def write_line(self, line: bytes) -> None:  # pragma: no cover - never called
        raise AssertionError("the metrics test never drives the transport")

    async def aclose(self) -> None:  # pragma: no cover - never called
        raise AssertionError("the metrics test never drives the transport")


# ---------------------------------------------------------------------------
# Wiring: manager turns, threads, restarts
# ---------------------------------------------------------------------------


def _manager():
    """A manager wired to nothing: every test below drives its BOOKKEEPING only,
    so no process is ever spawned and no `CODEX_HOME` is ever created."""
    from server.codex import schema
    from server.codex.capabilities import CodexAvailability
    from server.codex.manager import CodexRunnerManager
    from server.codex.redaction import Redacted

    async def credentials(account: str) -> schema.ExternalAuthLoginParams:
        return schema.ExternalAuthLoginParams(
            access_token=Redacted("unused"),
            chatgpt_account_id=Redacted("unused"),
            chatgpt_plan_type="pro",
        )

    return CodexRunnerManager(
        availability=CodexAvailability.ready(
            binary_path="/usr/bin/codex", cli_version="codex-cli 0.153.2"
        ),
        codex_home_base="/tmp/ccswitch-codex-metrics-test",
        credential_supplier=credentials,
        home_factory=lambda base, account: base / account,
        external_auth_enabled=True,
    )


def test_turn_gauge_rises_on_begin_and_falls_on_finish() -> None:
    manager = _manager()
    account = "metrics-turns"
    before = m.codex_turns_active.value_for(account)

    token = manager.begin_turn(account, "th_1")
    assert m.codex_turns_active.value_for(account) == before + 1
    manager.finish_turn(token)
    assert m.codex_turns_active.value_for(account) == before


def test_reopening_a_turn_for_the_same_thread_counts_once() -> None:
    manager = _manager()
    account = "metrics-turns-dup"
    before = m.codex_turns_active.value_for(account)

    manager.begin_turn(account, "th_1")
    token = manager.begin_turn(account, "th_1")
    assert m.codex_turns_active.value_for(account) == before + 1
    manager.finish_turn(token)
    assert m.codex_turns_active.value_for(account) == before


def test_unregistering_a_thread_mid_turn_releases_the_turn_gauge() -> None:
    """`unregister_thread` retires the turn book entry too; the gauge must follow
    it, or a driver torn down mid-turn leaks a turn forever."""
    manager = _manager()
    account = "metrics-teardown"
    before_turns = m.codex_turns_active.value_for(account)
    before_threads = m.codex_threads_active.value_for(account)

    manager.register_thread(account, "th_1", lambda notice: None)
    manager.begin_turn(account, "th_1")
    assert m.codex_threads_active.value_for(account) == before_threads + 1
    assert m.codex_turns_active.value_for(account) == before_turns + 1

    manager.unregister_thread(account, "th_1")
    assert m.codex_threads_active.value_for(account) == before_threads
    assert m.codex_turns_active.value_for(account) == before_turns


def test_registering_the_same_thread_twice_counts_one_thread() -> None:
    manager = _manager()
    account = "metrics-reattach"
    before = m.codex_threads_active.value_for(account)

    manager.register_thread(account, "th_1", lambda notice: None)
    manager.register_thread(account, "th_1", lambda notice: None)
    assert m.codex_threads_active.value_for(account) == before + 1
    manager.unregister_thread(account, "th_1")
    assert m.codex_threads_active.value_for(account) == before


def test_a_crash_that_wipes_the_turn_book_also_releases_the_gauge() -> None:
    """`_handle_exit` drops the account's whole turn book; `_close_all_turns` is
    the one place that pop and the gauge decrement stay together."""
    manager = _manager()
    account = "metrics-crash"
    before = m.codex_turns_active.value_for(account)

    manager.begin_turn(account, "th_1")
    manager.begin_turn(account, "th_2")
    assert m.codex_turns_active.value_for(account) == before + 2

    manager._close_all_turns(account)
    assert m.codex_turns_active.value_for(account) == before
    assert manager.active_turns(account) == ()


# ---------------------------------------------------------------------------
# Wiring: approval latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_latency_is_measured_and_separates_unrouted_requests() -> None:
    from server.codex import schema
    from server.codex.manager import CodexRunnerMissingError

    manager = _manager()
    account = "metrics-approvals"
    method = schema.METHOD_COMMAND_EXECUTION_APPROVAL
    params = _approval_params()

    before_decided = m.codex_approval_ms.count_for("decided")
    before_unrouted = m.codex_approval_ms.count_for("unrouted")

    # No sink registered → the request is refused, never answered. An approval
    # nobody owns must not become an allow, so it gets its own outcome label
    # rather than being timed as a decision.
    with pytest.raises(CodexRunnerMissingError):
        await manager._on_approval(account, method, params)
    assert m.codex_approval_ms.count_for("unrouted") == before_unrouted + 1
    assert m.codex_approval_ms.count_for("decided") == before_decided

    async def decide(_method, _params):
        await asyncio.sleep(0.01)
        return schema.CommandExecutionApprovalResponse(decision="decline")

    manager.register_thread(
        account, params.thread_id, lambda notice: None, approvals=decide
    )
    await manager._on_approval(account, method, params)
    assert m.codex_approval_ms.count_for("decided") == before_decided + 1
    assert m.codex_approval_ms.sum_for("decided") > 0.0


def _approval_params():
    """One real approval params object, built from the pinned schema types."""
    from server.codex import schema

    return schema.CommandExecutionApprovalParams(
        item_id="item_metrics",
        thread_id="th_metrics",
        turn_id="turn_metrics",
        started_at_ms=0,
        command="true",
        cwd="/tmp",
        available_decisions=("accept", "decline"),
    )


# ---------------------------------------------------------------------------
# Wiring: quota read time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_good_quota_read_publishes_its_read_time(monkeypatch) -> None:
    from server.codex import quota as quota_mod

    account = "metrics-quota"
    fixed = 1_700_000_000.0
    reader = quota_mod.CodexQuotaReader(clock=lambda: fixed)

    class _Client:
        async def read_rate_limits(self):
            return _rate_limits()

    await reader.read(account, client=_Client())
    assert m.codex_quota_last_read_timestamp_s.value_for(account) == fixed


def _rate_limits():
    from server.codex import schema

    return schema.GetAccountRateLimitsResponse(
        rate_limits=schema.RateLimitSnapshot(
            primary=schema.RateLimitWindow(
                used_percent=10, window_duration_mins=300
            ),
        )
    )


# ---------------------------------------------------------------------------
# Wiring: authority health and credential generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authority_calls_separate_an_outage_from_a_refusal() -> None:
    """A refusal ("the authority said no") and an outage ("it did not answer")
    demand opposite operator responses, so they must not share an outcome label."""
    from server.codex.auth_client import AuthorityClient
    from server.codex.auth_types import (
        AuthorityError,
        AuthorityErrorCode,
        AuthorityUnavailable,
    )

    client = AuthorityClient.__new__(AuthorityClient)
    client._deadline_s = 1.0  # type: ignore[attr-defined]
    client._admin_deadline_s = 1.0  # type: ignore[attr-defined]

    outcomes = iter(
        [
            AuthorityUnavailable("no route to host"),
            AuthorityError(AuthorityErrorCode.UNKNOWN_ACCOUNT, "nope", account="a"),
        ]
    )

    async def fail(*args, **kwargs):
        raise next(outcomes)

    client._invoke = fail  # type: ignore[method-assign]

    before_out = m.codex_auth_requests_total.value("health", "unavailable")
    before_ref = m.codex_auth_requests_total.value("health", "unknown_account")

    for expected in (AuthorityUnavailable, AuthorityError):
        with pytest.raises(expected):
            await client._call("health", {})

    assert m.codex_auth_requests_total.value("health", "unavailable") == before_out + 1
    assert m.codex_auth_requests_total.value("health", "unknown_account") == before_ref + 1
    assert m.codex_auth_request_ms.count_for("health", "unavailable") >= 1


@pytest.mark.asyncio
async def test_an_issued_envelope_publishes_only_its_generation() -> None:
    from server.codex.auth_client import AuthorityClient

    account = "metrics-generation"
    client = AuthorityClient.__new__(AuthorityClient)
    client._deadline_s = 1.0  # type: ignore[attr-defined]
    client._admin_deadline_s = 1.0  # type: ignore[attr-defined]

    async def issue(*args, **kwargs):
        return {
            "envelope": {
                "account": account,
                "accessToken": "sk-" + "Z9y8X7w6V5u4T3s2R1q0",
                "chatgptAccountId": "acct_native",
                "generation": 4,
            }
        }

    client._invoke = issue  # type: ignore[method-assign]
    await client.envelope(account)

    assert m.codex_auth_generation.value_for(account) == 4.0
    # The generation says a rotation happened, never what rotated: no other field
    # of the envelope becomes a label value anywhere.
    assert "chatgpt_account_id" not in m.codex_auth_generation.labelnames
