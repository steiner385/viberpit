"""Host gh-credential health: classify once, back off, self-heal (#2052).

The condition under test is dobby's: gdm autologin never unlocks the GNOME login
keyring, gh's token lives only there, and `_run_gh` strips GITHUB_TOKEN/GH_TOKEN —
so from boot until a human types the passphrase, EVERY ambient gh call runs
unauthenticated. Writes 401, reads 403 against the 60/hr unauthenticated IP bucket.
"""
from __future__ import annotations

import logging

import pytest

from ccswitch import gh_credential as ghc, gitrepos


# The exact strings gh emits. Byte-for-byte from the incident (issue #2052) —
# a classifier tuned to paraphrases is a classifier that does not fire.
STDERR_401 = "gh: Requires authentication (HTTP 401)"
STDERR_UNAUTH_403 = (
    "gh: API rate limit exceeded for 203.0.113.7. (But here's the good news: "
    "Authenticated requests get a higher rate limit. Check out the documentation "
    "for more details.) (HTTP 403)"
)
# The AUTHENTICATED over-limit message. Same endpoint, same status, opposite
# meaning: the credential is fine and the host is merely busy.
STDERR_AUTHED_403 = "gh: API rate limit exceeded for user ID 1234567. (HTTP 403)"
# gh with NO host config at all — a freshly provisioned runner before someone has
# run `gh auth login` into it. Captured from the real binary: it short-circuits
# before making a request, so it never produces the 401 above and, unclassified,
# every loop on that host retries at full tick rate forever with no signal.
STDERR_NOT_LOGGED_IN = (
    "gh: To get started with GitHub CLI, please run:  gh auth login\n"
    "Alternatively, populate the GH_TOKEN environment variable with a GitHub API "
    "authentication token."
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def health():
    clock = FakeClock()
    h = ghc.GhCredentialHealth(probe_interval_s=60.0, clock=clock)
    h.clock = clock  # test handle
    return h


@pytest.fixture(autouse=True)
def _reset_host_health():
    """The host latch is process-global; never let one test leak into the next."""
    ghc.host_health().reset()
    yield
    ghc.host_health().reset()


# --------------------------------------------------------------------------
# classify_gh_failure — the discriminator
# --------------------------------------------------------------------------

def test_classifies_401_as_unauthenticated():
    assert ghc.classify_gh_failure(STDERR_401) == ghc.UNAUTHENTICATED


def test_classifies_ip_shaped_rate_limit_as_unauthenticated():
    assert ghc.classify_gh_failure(STDERR_UNAUTH_403) == ghc.UNAUTHENTICATED


def test_classifies_a_host_with_no_gh_config_at_all():
    """The onboarding shape: gh has no host entry, so it never even attempts the
    call and never emits the 401. Unambiguous — that message cannot mean anything
    except "there is no ambient credential on this host"."""
    assert ghc.classify_gh_failure(STDERR_NOT_LOGGED_IN) == ghc.UNAUTHENTICATED


def test_not_logged_in_latches_and_backs_off_like_the_401(health):
    health.record(1, STDERR_NOT_LOGGED_IN)
    assert health.snapshot().unauthenticated is True
    assert health.allow_attempt() is False


def test_does_not_classify_authenticated_over_limit():
    """`for user ID N` means the token worked. Latching here would back off the
    whole fleet for an ordinary busy hour."""
    assert ghc.classify_gh_failure(STDERR_AUTHED_403) is None


@pytest.mark.parametrize("stderr", [
    "",
    "gh: Not Found (HTTP 404)",
    "gh: Validation Failed (HTTP 422)",
    "gh: Bad credentials (HTTP 401)",  # a real bad token, not a missing one
    "error: could not read Username for 'https://github.com'",
])
def test_leaves_unrelated_failures_unclassified(stderr):
    assert ghc.classify_gh_failure(stderr) is None


def test_classifies_our_own_suppressed_marker():
    """A suppressed call must read as the SAME condition downstream, so callers
    collapse it into the one signal rather than minting a second one."""
    assert ghc.classify_gh_failure(ghc.SUPPRESSED_STDERR) == ghc.UNAUTHENTICATED


# --------------------------------------------------------------------------
# GhCredentialHealth — latch, back-off, self-heal
# --------------------------------------------------------------------------

def test_starts_clean_and_allows_every_attempt(health):
    assert health.snapshot().unauthenticated is False
    assert all(health.allow_attempt() for _ in range(10))


def test_unrelated_failures_never_latch(health):
    for _ in range(5):
        health.record(1, "gh: Not Found (HTTP 404)")
    assert health.snapshot().unauthenticated is False


def test_first_unauthenticated_failure_latches(health):
    health.record(1, STDERR_401)
    snap = health.snapshot()
    assert snap.unauthenticated is True
    assert snap.failures == 1


def test_latched_host_suppresses_calls_until_the_probe_interval(health):
    health.record(1, STDERR_401)
    # Everything inside the window is suppressed: this is the whole point —
    # 20 loop ticks must not spend 20 requests from a 60/hr bucket.
    assert [health.allow_attempt() for _ in range(20)] == [False] * 20
    assert health.snapshot().suppressed == 20


def test_one_probe_is_allowed_through_per_interval(health):
    health.record(1, STDERR_401)
    assert health.allow_attempt() is False
    health.clock.advance(60.0)
    assert health.allow_attempt() is True, "the probe must be let through"
    assert health.allow_attempt() is False, "only ONE probe per interval"


def test_a_successful_probe_clears_the_latch(health):
    health.record(1, STDERR_401)
    health.clock.advance(60.0)
    assert health.allow_attempt() is True
    health.record(0, "")
    assert health.snapshot().unauthenticated is False
    assert health.allow_attempt() is True


def test_a_failed_probe_keeps_the_latch_and_restarts_the_window(health):
    health.record(1, STDERR_401)
    health.clock.advance(60.0)
    health.allow_attempt()
    health.record(1, STDERR_401)
    assert health.snapshot().unauthenticated is True
    assert health.allow_attempt() is False


def test_an_unrelated_failure_does_not_clear_the_latch(health):
    """Only a *successful* call proves the credential works. A 404 while latched
    says nothing, and clearing on it would resume the storm."""
    health.record(1, STDERR_401)
    health.clock.advance(60.0)
    health.allow_attempt()
    health.record(1, "gh: Not Found (HTTP 404)")
    assert health.snapshot().unauthenticated is True


def test_snapshot_reports_how_long_the_condition_has_held(health):
    health.record(1, STDERR_401)
    health.clock.advance(325.0)
    assert health.snapshot().for_s == pytest.approx(325.0)


def test_snapshot_for_s_is_none_when_healthy(health):
    assert health.snapshot().for_s is None


def test_latch_logs_once_not_once_per_failing_call(health, caplog):
    with caplog.at_level(logging.WARNING, logger=ghc.log.name):
        for _ in range(8):
            health.record(1, STDERR_401)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "host_gh_unauthenticated" in warnings[0].getMessage()


def test_a_standing_condition_re_reports_on_the_heartbeat(health, caplog):
    """A latch that reports once per process says "nothing is wrong" all day.
    The window is what lets the journal answer "is this STILL happening?"."""
    with caplog.at_level(logging.WARNING, logger=ghc.log.name):
        health.record(1, STDERR_401)
        health.clock.advance(ghc.REPORT_INTERVAL_S - 1)
        health.record(1, STDERR_401)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        health.clock.advance(2)
        health.record(1, STDERR_401)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_recovery_is_reported_at_info_so_it_files_no_issue(health, caplog):
    health.record(1, STDERR_401)
    with caplog.at_level(logging.INFO, logger=ghc.log.name):
        health.record(0, "")
    records = [r for r in caplog.records if "restored" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_relatching_after_a_recovery_reports_again(health, caplog):
    health.record(1, STDERR_401)
    health.record(0, "")
    with caplog.at_level(logging.WARNING, logger=ghc.log.name):
        health.record(1, STDERR_401)
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_kill_switch_keeps_the_signal_but_stops_the_back_off(monkeypatch):
    monkeypatch.setenv("VIBERPIT_GH_UNAUTH_BACKOFF", "false")
    h = ghc.GhCredentialHealth(probe_interval_s=60.0, clock=FakeClock())
    h.record(1, STDERR_401)
    assert h.snapshot().unauthenticated is True, "still observed"
    assert h.allow_attempt() is True, "but nothing is held back"


# --------------------------------------------------------------------------
# _run_gh integration — the chokepoint
# --------------------------------------------------------------------------

def _fake_run(returncode: int, stderr: str = "", stdout: str = ""):
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)

        class P:
            pass
        p = P()
        p.returncode = returncode
        p.stdout = stdout
        p.stderr = stderr
        return p

    run.calls = calls
    return run


def test_run_gh_records_an_unauthenticated_failure(monkeypatch):
    monkeypatch.setattr(gitrepos.subprocess, "run", _fake_run(1, STDERR_401))
    gitrepos._run_gh(["api", "user"])
    assert ghc.host_health().snapshot().unauthenticated is True


def test_run_gh_suppresses_the_next_call_without_spawning_gh(monkeypatch):
    run = _fake_run(1, STDERR_401)
    monkeypatch.setattr(gitrepos.subprocess, "run", run)
    gitrepos._run_gh(["api", "user"])
    proc = gitrepos._run_gh(["api", "repos/o/n/issues", "-f", "body=x"])
    assert len(run.calls) == 1, "the second call must not reach the network"
    assert proc.returncode != 0
    assert ghc.SUPPRESSED_STDERR in proc.stderr


def test_run_gh_does_not_suppress_an_app_token_call(monkeypatch):
    """An App installation token never touches the keyring, so a locked keyring
    must not stop the paths that already authenticate per-owner (#913)."""
    run = _fake_run(1, STDERR_401)
    monkeypatch.setattr(gitrepos.subprocess, "run", run)
    gitrepos._run_gh(["api", "user"])
    gitrepos._run_gh(["api", "user"], token="ghs_APP")
    assert len(run.calls) == 2


def test_an_app_token_401_never_latches_the_host(monkeypatch):
    """A revoked App token is that owner's problem, not the host credential's."""
    monkeypatch.setattr(gitrepos.subprocess, "run", _fake_run(1, STDERR_401))
    gitrepos._run_gh(["api", "user"], token="ghs_EXPIRED")
    assert ghc.host_health().snapshot().unauthenticated is False


def test_run_gh_success_clears_a_latch(monkeypatch):
    monkeypatch.setattr(gitrepos.subprocess, "run", _fake_run(1, STDERR_401))
    gitrepos._run_gh(["api", "user"])
    assert ghc.host_health().snapshot().unauthenticated is True
    ghc.host_health().reset()  # stand in for the probe interval elapsing
    monkeypatch.setattr(gitrepos.subprocess, "run", _fake_run(0, "", "{}"))
    gitrepos._run_gh(["api", "user"])
    assert ghc.host_health().snapshot().unauthenticated is False


def test_suppressed_result_keeps_the_completedprocess_shape(monkeypatch):
    """Every one of the ~40 call sites reads `.returncode`, `.stdout`, `.stderr`.
    A suppressed call must be indistinguishable in shape from a failed one."""
    monkeypatch.setattr(gitrepos.subprocess, "run", _fake_run(1, STDERR_401))
    gitrepos._run_gh(["api", "user"])
    proc = gitrepos._run_gh(["api", "user"])
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert isinstance(proc.stderr, str)
    assert proc.args == ["gh", "api", "user"]
