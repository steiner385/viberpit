"""Tests for the shared helper-call backoff + circuit breaker."""
import asyncio
from types import SimpleNamespace

import pytest
from viberpit import ledger as _ledger
from viberpit.quota import anthropic_snapshot

from server.sessions import helper_call
from server.sessions.helper_call import run_helper_call
from server.tests.fakes.helper_streams import OPAQUE_SDK_ERROR, org_denied_messages


@pytest.fixture(autouse=True)
def _clean_breakers():
    helper_call.reset_breakers()
    yield
    helper_call.reset_breakers()


class Clock:
    """Manually-advanced monotonic clock for deterministic breaker tests."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _sleeps_recorded():
    """A fake _sleep that records delays instead of blocking."""
    delays = []

    async def _sleep(d):
        delays.append(d)

    return delays, _sleep


def _assistant(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _result(api_error_status):
    return SimpleNamespace(api_error_status=api_error_status, is_error=True, subtype="success")


def _stream_factory(*, yield_msgs=(), raise_exc=None, counter=None):
    """Return a make_stream() producing a fresh async iterator each call."""
    def make_stream():
        if counter is not None:
            counter["n"] += 1

        async def gen():
            for m in yield_msgs:
                yield m
            if raise_exc is not None:
                raise raise_exc
        return gen()
    return make_stream


def _run(make_stream, **kw):
    kw.setdefault("subsystem", "test")
    kw.setdefault("account_key", "/cfg/acct1")
    return asyncio.run(run_helper_call(make_stream, **kw))


def test_success_returns_messages_and_keeps_lane_closed():
    out = _run(_stream_factory(yield_msgs=[_assistant("hi")]))
    assert out.error is None and out.skipped is False
    assert [getattr(m.content[0], "text") for m in out.messages] == ["hi"]


def test_rate_limit_is_not_retried_and_opens_lane_immediately():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}
    # SDK shape for a 429: yields a ResultMessage with api_error_status, then raises.
    ms = _stream_factory(
        yield_msgs=[_assistant("Rate limit exceeded. API Error: 429"), _result(429)],
        raise_exc=RuntimeError("Claude Code returned an error result: success"),
        counter=counter,
    )
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.error is not None
    assert out.failure_kind == "rate_limit"
    assert out.api_error_status == 429
    assert counter["n"] == 1          # NOT retried
    assert delays == []               # no backoff sleep
    # Lane opened immediately — a second call is skipped without invoking the stream.
    counter["n"] = 0
    out2 = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                       _now=clock, _sleep=sleep))
    assert out2.skipped is True
    assert counter["n"] == 0


def test_transient_overload_is_retried_then_fails():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}
    ms = _stream_factory(raise_exc=RuntimeError("API Error: 503 overloaded"), counter=counter)
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.failure_kind == "overloaded"
    assert counter["n"] == 4          # initial + 3 retries
    assert delays == [1.0, 2.0, 4.0]  # exponential backoff


def test_circuit_opens_after_five_consecutive_other_failures():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    ms = _stream_factory(raise_exc=RuntimeError("boom"))  # classifies as "other", not retried
    for _ in range(5):
        out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                          _now=clock, _sleep=sleep))
        assert out.skipped is False
    # Sixth call: lane is open, skipped.
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is True


def test_half_open_trial_after_cooldown_closes_on_success():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    # auth_401 opens its lane immediately like rate_limit does, but (unlike
    # rate_limit, since #2410) is not an account-wide block, so the lane's own
    # 60s cooldown is what gates the next call — the thing under test here.
    fail = _stream_factory(raise_exc=RuntimeError("API Error: 401 invalid authentication"))
    asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/acct1",
                                _now=clock, _sleep=sleep))
    assert asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/acct1",
                                       _now=clock, _sleep=sleep)).skipped is True
    # After the cooldown, a half-open trial is allowed and its success closes the lane.
    clock.advance(61)
    ok = _stream_factory(yield_msgs=[_assistant("recovered")])
    out = asyncio.run(run_helper_call(ok, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is False and out.error is None


def test_cli_not_logged_in_opens_the_lane_immediately():
    # #2758: a helper's fresh CLI with no usable credential classifies auth_stale;
    # every further call on that account would fail the same way.
    clock = Clock()
    _, sleep = _sleeps_recorded()
    fail = _stream_factory(raise_exc=RuntimeError(
        "Claude Code returned an error result: success detail="
        "'authentication_failed | Not logged in · Please run /login'"))
    out = asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.failure_kind == "auth_stale"
    assert asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/acct1",
                                       _now=clock, _sleep=sleep)).skipped is True


def test_lanes_are_isolated_per_account_and_subsystem():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    # auth_401, not rate_limit: since #2410 a rate limit bars the whole account
    # (see the rate-limit block tests below), so it is the wrong failure kind to
    # demonstrate plain per-(subsystem, account) lane isolation with.
    fail = _stream_factory(raise_exc=RuntimeError("401 invalid authentication"))
    asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/bad",
                                _now=clock, _sleep=sleep))
    # A different account's lane is unaffected.
    ok = _stream_factory(yield_msgs=[_assistant("fine")])
    out = asyncio.run(run_helper_call(ok, subsystem="test", account_key="/cfg/good",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is False and out.error is None
    # Same account, different subsystem is also its own lane.
    out2 = asyncio.run(run_helper_call(ok, subsystem="other", account_key="/cfg/bad",
                                       _now=clock, _sleep=sleep))
    assert out2.skipped is False


def test_per_attempt_timeout_fails_open_without_retry():
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}

    def make_stream():
        counter["n"] += 1

        async def gen():
            await asyncio.sleep(10)
            yield _assistant("never")
        return gen()

    out = asyncio.run(run_helper_call(make_stream, subsystem="test", account_key="/cfg/acct1",
                                      per_attempt_timeout=0.01, _now=clock, _sleep=sleep))
    assert out.error is not None
    assert out.failure_kind == "timeout"
    assert counter["n"] == 1   # a wedged call is not retried
    assert delays == []


# --------------------------------------------------------------------------- #
# Durable authorization denial (#1820).                                        #
#                                                                              #
# Live 2026-09-22: one enabled, ledger-healthy Anthropic account answered every #
# helper call with HTTP 403 oauth_not_allowed_for_organization. It is the first #
# account pick_helper_config_dir() returns, so EVERY helper subsystem routed to #
# it and failed; autopilot logged 713 "PERSISTENT FAILURE" records whose only   #
# text was the SDK's opaque "returned an error result: success".               #
# --------------------------------------------------------------------------- #

def _org_denied_stream(*, status=403, counter=None):
    return _stream_factory(
        yield_msgs=org_denied_messages(status),
        raise_exc=RuntimeError(OPAQUE_SDK_ERROR),
        counter=counter)


@pytest.mark.parametrize("status", [403, None])
def test_org_denial_is_read_off_the_messages_not_the_opaque_exception(status):
    # status=None is the shape without the CLI's api_error_status: the denial is
    # then knowable only from the API-error assistant message.
    clock = Clock()
    _, sleep = _sleeps_recorded()
    out = asyncio.run(run_helper_call(_org_denied_stream(status=status),
                                      subsystem="test", account_key="/cfg/denied",
                                      _now=clock, _sleep=sleep))
    assert out.failure_kind == "auth_forbidden"
    assert out.api_error_status == status
    assert out.error_text is not None and "organization has disabled" in out.error_text


def test_a_denied_account_is_blocked_for_every_helper_subsystem():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    counter = {"n": 0}
    asyncio.run(run_helper_call(_org_denied_stream(counter=counter), subsystem="decide",
                                account_key="/cfg/denied", _now=clock, _sleep=sleep))
    assert counter["n"] == 1                       # never retried
    assert helper_call.is_helper_account_blocked("/cfg/denied", _now=clock)
    # A *different* subsystem on the same account is skipped without a spawn — the
    # denial is account-wide, so a per-lane breaker alone would let every other
    # helper re-learn it the hard way.
    ok = _stream_factory(yield_msgs=[_assistant("fine")], counter=counter)
    out = asyncio.run(run_helper_call(ok, subsystem="title", account_key="/cfg/denied",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is True and counter["n"] == 1
    # Another account is untouched.
    out2 = asyncio.run(run_helper_call(ok, subsystem="title", account_key="/cfg/good",
                                       _now=clock, _sleep=sleep))
    assert out2.skipped is False and out2.error is None


def test_a_blocked_account_is_not_helper_ready_so_the_pickers_fail_over(monkeypatch):
    import contextlib

    # A ledger that reports the account perfectly healthy — which is exactly what
    # the real one said throughout #1820. Readiness must turn on what the calls
    # learned, not on what the ledger believes.
    @contextlib.contextmanager
    def _healthy_ledger():
        yield {}
    monkeypatch.setattr(helper_call, "_account_cfg_dir", lambda name: f"/cfg/{name}")
    monkeypatch.setattr(_ledger, "locked_ledger", _healthy_ledger)
    monkeypatch.setattr(_ledger, "is_account_enabled", lambda led, name: True)
    monkeypatch.setattr(_ledger, "get_status", lambda led, name: "ok")
    assert helper_call.is_account_helper_ready("denied") is True

    # Real monotonic clock here: is_account_helper_ready() is the pickers' door
    # and takes no clock injection.
    _, sleep = _sleeps_recorded()
    asyncio.run(run_helper_call(_org_denied_stream(), subsystem="decide",
                                account_key="/cfg/denied", _sleep=sleep))
    assert helper_call.is_account_helper_ready("denied") is False
    assert helper_call.is_account_helper_ready("other") is True


def test_org_denied_ledger_status_alone_is_not_helper_ready(monkeypatch):
    """The in-memory 30-min block above resets on a daemon restart, but a live
    session's ledger flip to 'org-denied' (#2244) persists — so readiness must
    also fail on that status directly, with no block in play."""
    import contextlib

    @contextlib.contextmanager
    def _org_denied_ledger():
        yield {}
    monkeypatch.setattr(helper_call, "_account_cfg_dir", lambda name: f"/cfg/{name}")
    monkeypatch.setattr(_ledger, "locked_ledger", _org_denied_ledger)
    monkeypatch.setattr(_ledger, "is_account_enabled", lambda led, name: True)
    monkeypatch.setattr(_ledger, "get_status", lambda led, name: "org-denied")
    assert helper_call.is_account_helper_ready("kindash") is False


def test_the_account_block_expires_so_a_restored_account_recovers():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    asyncio.run(run_helper_call(_org_denied_stream(), subsystem="decide",
                                account_key="/cfg/denied", _now=clock, _sleep=sleep))
    clock.advance(helper_call._AUTH_BLOCK_S + 1)
    assert helper_call.is_helper_account_blocked("/cfg/denied", _now=clock) is False
    ok = _stream_factory(yield_msgs=[_assistant("fine")])
    out = asyncio.run(run_helper_call(ok, subsystem="decide", account_key="/cfg/denied",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is False and out.error is None


# --------------------------------------------------------------------------- #
# Account-wide rate-limit block (#2410).                                       #
#                                                                              #
# Live 2026-09-25: an account hit its WEEKLY limit; only the per-(subsystem,   #
# account) lane opened (60s cooldown), so every call more than 60s after the   #
# last one re-picked the same exhausted account and failed again — logging a   #
# fresh WARNING each time until the weekly reset, days away. Other accounts    #
# with headroom were never tried.                                              #
# --------------------------------------------------------------------------- #

def _rate_limited_stream(*, counter=None):
    # "rate_limit" (the classifier's own marker text) plus the CLI's real-world
    # weekly-limit sentence from #2410's report.
    return _stream_factory(
        raise_exc=RuntimeError(
            "rate_limit | You've hit your weekly limit · resets 2am (America/New_York)"),
        counter=counter)


def test_rate_limit_bars_the_whole_account_so_pickers_fail_over(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def _healthy_ledger():
        yield {}
    monkeypatch.setattr(helper_call, "_account_cfg_dir", lambda name: f"/cfg/{name}")
    monkeypatch.setattr(_ledger, "locked_ledger", _healthy_ledger)
    monkeypatch.setattr(_ledger, "is_account_enabled", lambda led, name: True)
    monkeypatch.setattr(_ledger, "get_status", lambda led, name: "ok")

    _, sleep = _sleeps_recorded()
    asyncio.run(run_helper_call(_rate_limited_stream(), subsystem="assess_model",
                                account_key="/cfg/limited", _sleep=sleep))
    assert helper_call.is_account_helper_ready("limited") is False
    assert helper_call.is_account_helper_ready("other") is True


def test_rate_limit_two_calls_61s_apart_fail_over_to_a_second_account():
    """The #2410 repro: a call more than the lane's 60s cooldown after the first
    failure must NOT re-pick the same rate-limited account — only the (much
    longer) account-wide block, not the lane, may still be gating it."""
    clock = Clock()
    _, sleep = _sleeps_recorded()
    counter = {"n": 0}
    fail = _rate_limited_stream(counter=counter)
    asyncio.run(run_helper_call(fail, subsystem="assess_model", account_key="/cfg/acct1",
                                _now=clock, _sleep=sleep))
    assert counter["n"] == 1
    clock.advance(61)  # past the lane's 60s cooldown — the OLD bug re-tried here
    out = asyncio.run(run_helper_call(fail, subsystem="assess_model", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.skipped is True and out.skip_reason == "account-blocked"
    assert counter["n"] == 1   # the stream was never invoked a second time


def test_rate_limit_block_falls_back_to_a_bounded_ttl_with_no_cached_reset():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    # No usage is cached for "acct1" in the real ledger, so _reset_deadline_s
    # finds nothing and the block must fall back to _RATE_LIMIT_BLOCK_S rather
    # than blocking forever or not at all.
    asyncio.run(run_helper_call(_rate_limited_stream(), subsystem="test",
                                account_key="/cfg/acct1", _now=clock, _sleep=sleep))
    clock.advance(helper_call._RATE_LIMIT_BLOCK_S - 1)
    assert helper_call.is_helper_account_blocked("/cfg/acct1", _now=clock) is True
    clock.advance(2)
    assert helper_call.is_helper_account_blocked("/cfg/acct1", _now=clock) is False


def test_rate_limit_block_uses_the_ledger_cached_reset_time(monkeypatch):
    import contextlib
    from datetime import datetime, timedelta, timezone

    # _reset_deadline_s measures against REAL wall-clock time (the deadline is a
    # UTC timestamp from the provider, not something the monotonic test clock
    # can stand in for), so resets_at must be anchored to actual now.
    now_utc = datetime.now(timezone.utc)
    resets_at = now_utc + timedelta(hours=3)   # well short of the 1800s TTL fallback
    snapshot = anthropic_snapshot(0.0, None, 100.0, resets_at, False, fetched_at=now_utc)

    @contextlib.contextmanager
    def _ledger_cm():
        yield {}
    monkeypatch.setattr(_ledger, "locked_ledger", _ledger_cm)
    monkeypatch.setattr(_ledger, "stale_usage",
                        lambda led, name, now, max_age: (snapshot, now_utc))

    # "acct1" (the ledger name) comes straight off /cfg/acct1's last path
    # component — no account registry involved.
    deadline_s = helper_call._reset_deadline_s("/cfg/acct1")
    assert deadline_s is not None and abs(deadline_s - 3 * 3600) < 5

    clock = Clock()
    _, sleep = _sleeps_recorded()
    asyncio.run(run_helper_call(_rate_limited_stream(), subsystem="test",
                                account_key="/cfg/acct1", _now=clock, _sleep=sleep))
    clock.advance(deadline_s - 1)
    assert helper_call.is_helper_account_blocked("/cfg/acct1", _now=clock) is True
    clock.advance(2)
    assert helper_call.is_helper_account_blocked("/cfg/acct1", _now=clock) is False


def test_rate_limit_logs_one_info_per_block_not_per_call(caplog):
    clock = Clock()
    _, sleep = _sleeps_recorded()
    fail = _rate_limited_stream()
    with caplog.at_level("INFO", logger=helper_call.log.name):
        asyncio.run(run_helper_call(fail, subsystem="assess_model", account_key="/cfg/acct1",
                                    _now=clock, _sleep=sleep))
        # Further calls within the block are skipped before any stream is made
        # and must not add another WARNING (#2410: this repeated every ~60s).
        for _ in range(3):
            out = asyncio.run(run_helper_call(fail, subsystem="assess_model",
                                              account_key="/cfg/acct1",
                                              _now=clock, _sleep=sleep))
            assert out.skipped is True and out.skip_reason == "account-blocked"
    rate_limit_warnings = [r for r in caplog.records if "rate-limited" in r.getMessage()]
    assert len(rate_limit_warnings) == 1
    # INFO, not WARNING: routine quota exhaustion must not auto-file an issue.
    assert rate_limit_warnings[0].levelname == "INFO"


def test_org_denial_block_still_logs_at_warning(caplog):
    clock = Clock()
    _, sleep = _sleeps_recorded()
    with caplog.at_level("INFO", logger=helper_call.log.name):
        asyncio.run(run_helper_call(_org_denied_stream(), subsystem="decide",
                                    account_key="/cfg/denied2", _now=clock, _sleep=sleep))
    refused = [r for r in caplog.records if "refused authorization" in r.getMessage()]
    assert [r.levelname for r in refused] == ["WARNING"]


def test_a_partial_model_reply_is_never_mistaken_for_an_error_detail():
    # Only error-shaped text feeds the classifier and the log: a normal assistant
    # reply that happens to mention 401 must not be read as an auth failure, and
    # must not be echoed into a log line as "the CLI's error".
    clock = Clock()
    _, sleep = _sleeps_recorded()
    ms = _stream_factory(yield_msgs=[_assistant("the fix is to retry on HTTP 401")],
                         raise_exc=RuntimeError("boom"))
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      _now=clock, _sleep=sleep))
    assert out.failure_kind == "other"
    assert out.error_text is None


# --------------------------------------------------------------------------- #
# helper_cwd: where a stateless one-shot helper runs.                          #
#                                                                              #
# Live finding 2026-09-11: the decider, composer and PR-intent classifier ran  #
# with cwd=os.getcwd() — the daemon's own checkout. Two effects, both verified #
# against the running system: (1) Claude Code loaded THAT repo's CLAUDE.md    #
# into every autopilot judgement for every governed repo ("ALWAYS flip        #
# auto-merge on…"), (2) ~12,400 helper transcripts in 14 days landed in the   #
# checkout's ~/.claude/projects entry, so a plain `claude --resume` there      #
# listed autopilot's prompts as if they were the operator's own sessions.     #
# --------------------------------------------------------------------------- #

def test_helper_cwd_honours_env_override_and_creates_it(tmp_path, monkeypatch):
    target = tmp_path / "scratch" / "cwd"
    monkeypatch.setenv("VIBERPIT_HELPER_CWD", str(target))
    got = helper_call.helper_cwd()
    assert got == str(target)
    assert target.is_dir()


def test_helper_cwd_default_lives_under_the_cache_not_a_checkout(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.delenv("VIBERPIT_HELPER_CWD", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    got = Path(helper_call.helper_cwd())
    assert got == tmp_path / ".cache/ccswitch/helper-cwd"
    assert got.is_dir()
    assert not (got / "CLAUDE.md").exists() and not (got / ".git").exists()


_STATELESS_HELPER_MODULES = (
    "server/automations/autopilot_decider.py",
    "server/automations/nudge_composer.py",
    "server/automations/pr_intent.py",
    "server/automations/model_pick.py",
    "server/automations/backlog_grouping.py",
    "server/sessions/prompt_refiner.py",
    "server/sessions/open_question.py",
    # The Codex one-shot helpers (refine / suggest / goal) build an AgentDriverConfig
    # whose cwd Codex roots its sandbox AND its AGENTS.md lookup in — same shape.
    "server/sessions/orchestrator.py",
)


def test_no_stateless_helper_runs_in_the_daemon_cwd():
    """Every one-shot helper must run in ``helper_cwd()`` — never the daemon's own
    working directory (a repo checkout whose CLAUDE.md then leaks into the call)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in _STATELESS_HELPER_MODULES:
        src = (root / rel).read_text(encoding="utf-8")
        assert "os.getcwd()" not in src, f"{rel} still runs in the daemon cwd"
        assert "helper_cwd()" in src, f"{rel} does not route through helper_cwd()"


def test_skip_reason_tells_a_cooled_lane_from_a_barred_account():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    # Five "other" failures open only the lane (below _FAIL_THRESHOLD's kinds
    # that open immediately, and not a kind that bars the account).
    fail = _stream_factory(raise_exc=RuntimeError("boom"))
    for _ in range(5):
        asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/limited",
                                    _now=clock, _sleep=sleep))
    out = asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/limited",
                                      _now=clock, _sleep=sleep))
    assert (out.skipped, out.skip_reason) == (True, "circuit-open")

    asyncio.run(run_helper_call(_org_denied_stream(), subsystem="test",
                                account_key="/cfg/denied", _now=clock, _sleep=sleep))
    out2 = asyncio.run(run_helper_call(fail, subsystem="test", account_key="/cfg/denied",
                                       _now=clock, _sleep=sleep))
    assert (out2.skipped, out2.skip_reason) == (True, "account-blocked")


def test_network_failure_is_retried_and_reported_as_network():
    """#2623: EAI_AGAIN classified "other" — never retried, and every caller
    logged it as a WARNING (one auto-error issue per helper subsystem)."""
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}
    ms = _stream_factory(
        # The CLI's API-error assistant message, as the issue's detail shows it.
        yield_msgs=[SimpleNamespace(error="server_error", content=[SimpleNamespace(
            text="API Error: Can't reach the API server — check your internet "
                 "or DNS (EAI_AGAIN)")])],
        raise_exc=RuntimeError("Claude Code returned an error result: success"),
        counter=counter)
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      retries=1, _now=clock, _sleep=sleep))
    assert out.failure_kind == "network"
    assert counter["n"] == 2          # initial + 1 retry
    assert delays == [1.0]
    import logging
    assert out.log_level == logging.INFO


def test_a_defect_outcome_still_logs_at_warning():
    import logging
    out = _run(_stream_factory(raise_exc=RuntimeError("boom")))
    assert out.failure_kind == "other"
    assert out.log_level == logging.WARNING


def test_cli_startup_timeout_is_retried_once_and_logs_below_warning():
    """#2664: "Control request timeout: initialize" classified "other" — never
    retried, logged at WARNING. Each attempt can cost the SDK's 60s handshake
    window, so it gets one retry even when the caller allows more."""
    import logging
    clock = Clock()
    delays, sleep = _sleeps_recorded()
    counter = {"n": 0}
    ms = _stream_factory(raise_exc=Exception("Control request timeout: initialize"),
                         counter=counter)
    out = asyncio.run(run_helper_call(ms, subsystem="test", account_key="/cfg/acct1",
                                      retries=3, _now=clock, _sleep=sleep))
    assert out.failure_kind == "cli_startup_timeout"
    assert counter["n"] == 2          # initial + exactly 1 retry
    assert delays == [1.0]
    assert out.log_level == logging.INFO


def _healthy_for_envelope(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def _led():
        yield {}
    monkeypatch.setattr(helper_call, "_account_cfg_dir", lambda name: f"/cfg/{name}")
    monkeypatch.setattr(_ledger, "locked_ledger", _led)
    monkeypatch.setattr(_ledger, "is_account_enabled", lambda led, name: True)
    monkeypatch.setattr(_ledger, "get_status", lambda led, name: "ok")


def test_envelope_account_with_lapsed_lease_is_not_helper_ready(monkeypatch):
    """#2758: post-restart worker, lease lapsed -> oauth_env_for() == {} -> not ready."""
    from viberpit import accounts
    from server.sessions import account_auth
    _healthy_for_envelope(monkeypatch)
    monkeypatch.setattr(accounts, "runner_uses_envelope", lambda name: True)
    monkeypatch.setattr(account_auth, "oauth_env_for", lambda name: {})
    assert helper_call.is_account_helper_ready("rocky-acct") is False


def test_envelope_account_with_valid_lease_is_helper_ready(monkeypatch):
    from viberpit import accounts
    from server.sessions import account_auth
    _healthy_for_envelope(monkeypatch)
    monkeypatch.setattr(accounts, "runner_uses_envelope", lambda name: True)
    monkeypatch.setattr(account_auth, "oauth_env_for",
                        lambda name: {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert helper_call.is_account_helper_ready("rocky-acct") is True


def test_non_envelope_account_ignores_empty_token_env(monkeypatch):
    from viberpit import accounts
    from server.sessions import account_auth
    _healthy_for_envelope(monkeypatch)
    monkeypatch.setattr(accounts, "runner_uses_envelope", lambda name: False)
    monkeypatch.setattr(account_auth, "oauth_env_for", lambda name: {})
    assert helper_call.is_account_helper_ready("browser-oauth") is True


# #2953: a worker daemon wakes from a restart with every envelope-held account
# unlaunchable until the edge's next creds_sync, so the helper pickers find no
# account for a few minutes. Callers need to tell that self-healing window apart
# from a steady-state "no helper account" — this is the one predicate they ask.
def test_helper_credentials_pending_tracks_the_worker_lease(monkeypatch, tmp_path):
    from viberpit import credential_envelope as ce
    from server.sessions.helper_call import helper_credentials_pending

    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    monkeypatch.setenv("VIBERPIT_CLAUDE_CREDS_DIR", str(tmp_path))
    ce.reset_runner_store()
    try:
        assert helper_credentials_pending() is False  # nothing ever vended: pre-broker path
        env = ce.Envelope("a", ce.SETUP_TOKEN, "TOK", None, 1)
        ce.runner_store().sync([env], -1)  # held, lease lapsed — the restart window
        assert helper_credentials_pending() is True
        ce.runner_store().sync([env], 3600)  # the edge's creds_sync landed
        assert helper_credentials_pending() is False
        ce.runner_store().sync([env], -1)
        monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "primary")
        assert helper_credentials_pending() is False  # the primary holds no lease
    finally:
        ce.reset_runner_store()


def test_helper_credentials_pending_fails_to_false(monkeypatch):
    """An unreadable store must never turn a real outage into a silent deferral."""
    from viberpit import credential_envelope as ce
    from server.sessions.helper_call import helper_credentials_pending

    def boom():
        raise OSError("unreadable cache")
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    monkeypatch.setattr(ce, "runner_store", boom)
    assert helper_credentials_pending() is False
