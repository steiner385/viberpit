"""Tests for the shared helper-call backoff + circuit breaker."""
import asyncio
from types import SimpleNamespace

import pytest

from server.sessions import helper_call
from server.sessions.helper_call import run_helper_call


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
    fail = _stream_factory(raise_exc=RuntimeError("Rate limit 429"))
    # One 429 opens the lane immediately.
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


def test_lanes_are_isolated_per_account_and_subsystem():
    clock = Clock()
    _, sleep = _sleeps_recorded()
    fail = _stream_factory(raise_exc=RuntimeError("429 rate limit"))
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

_ORG_DENIED = ("Your organization has disabled Claude subscription access for Claude "
               "Code · Use an Anthropic API key instead, or ask your admin to enable "
               "access")


def _api_error_assistant(text, error="oauth_org_not_allowed"):
    """SDK shape for an API-error assistant message: ``.error`` is set and the
    text block carries the CLI's own sentence."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)], error=error)


def _org_denied_stream(*, status=403, counter=None):
    return _stream_factory(
        yield_msgs=[_api_error_assistant(_ORG_DENIED), _result(status)],
        raise_exc=RuntimeError("Claude Code returned an error result: success"),
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

    from ccswitch import ledger as _ledger

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

    from ccswitch import ledger as _ledger

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
    fail = _stream_factory(raise_exc=RuntimeError("Rate limit 429"))
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
