"""Host gh-credential health: classify once, back off, self-heal (#2052).

The condition under test is dobby's: gdm autologin never unlocks the GNOME login
keyring, gh's token lives only there, and `_run_gh` strips GITHUB_TOKEN/GH_TOKEN —
so from boot until a human types the passphrase, EVERY ambient gh call runs
unauthenticated. Writes 401, reads 403 against the 60/hr unauthenticated IP bucket.
"""
from __future__ import annotations

import logging

import pytest

from viberpit import gh_credential as ghc, gitrepos


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
# gh's client-side short-circuit against its own cached rate-limit state, rather
# than a server 403 round-trip — one word ("already") different from the two
# messages above. Byte-for-byte from issue #2169.
STDERR_AUTHED_403_ALREADY = "gh: API rate limit already exceeded for user ID 9727664."
STDERR_UNAUTH_403_ALREADY = "gh: API rate limit already exceeded for 203.0.113.7."
# gh with NO host config at all — a freshly provisioned runner before someone has
# run `gh auth login` into it. Captured from the real binary: it short-circuits
# before making a request, so it never produces the 401 above and, unclassified,
# every loop on that host retries at full tick rate forever with no signal.
STDERR_NOT_LOGGED_IN = (
    "gh: To get started with GitHub CLI, please run:  gh auth login\n"
    "Alternatively, populate the GH_TOKEN environment variable with a GitHub API "
    "authentication token."
)
# gh when the request never reached GitHub — byte-for-byte from the 2026-09-17
# outage that was filed as five separate auto-errors (#1922-#1927).
STDERR_UNREACHABLE = (
    "error connecting to api.github.com\n"
    "check your internet connection or https://githubstatus.com"
)
# GitHub's abuse/secondary limit, which names no subject at all.
STDERR_SECONDARY_403 = (
    "gh: You have exceeded a secondary rate limit. Please wait a few minutes "
    "before you try again. (HTTP 403)"
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


def test_classifies_ip_shaped_already_exceeded_as_unauthenticated():
    """gh's client-side short-circuit wording (#2169) — 'already' must not break
    the IP-shaped match any more than the user-ID one below."""
    assert ghc.classify_gh_failure(STDERR_UNAUTH_403_ALREADY) == ghc.UNAUTHENTICATED


def test_classifies_a_host_with_no_gh_config_at_all():
    """The onboarding shape: gh has no host entry, so it never even attempts the
    call and never emits the 401. Unambiguous — that message cannot mean anything
    except "there is no ambient credential on this host"."""
    assert ghc.classify_gh_failure(STDERR_NOT_LOGGED_IN) == ghc.UNAUTHENTICATED


def test_not_logged_in_latches_and_backs_off_like_the_401(health):
    health.record(1, STDERR_NOT_LOGGED_IN)
    assert health.snapshot().unauthenticated is True
    assert health.allow_attempt() is False


@pytest.mark.parametrize(
    "stderr", [STDERR_AUTHED_403, STDERR_AUTHED_403_ALREADY, STDERR_SECONDARY_403]
)
def test_an_authenticated_over_limit_is_its_own_condition(stderr):
    """`for user ID N` means the token worked — the opposite diagnosis from the
    IP-shaped 403, and the entire reason `_RE_UNAUTH_RATE_LIMIT` carries a
    negative lookahead. Naming it RATE_LIMITED keeps the log collapsible without
    ever confusing it for a missing credential."""
    assert ghc.classify_gh_failure(stderr) == ghc.RATE_LIMITED


def test_classifies_a_transport_failure_as_unreachable():
    assert ghc.classify_gh_failure(STDERR_UNREACHABLE) == ghc.UNREACHABLE


def test_either_half_of_the_transport_message_is_enough():
    """gh prints two lines and could reword one; matching them independently
    means a rewrapped dial error still classifies."""
    for half in STDERR_UNREACHABLE.splitlines():
        assert ghc.classify_gh_failure(half) == ghc.UNREACHABLE


@pytest.mark.parametrize("stderr", [STDERR_UNREACHABLE, STDERR_AUTHED_403,
                                    STDERR_SECONDARY_403])
def test_the_non_credential_conditions_never_latch(health, stderr):
    """The latch gates traffic, and neither waiting out a rate limit nor waiting
    for a network is helped by that — a connect error spends no budget, so
    backing off would only delay recovery. They are classified for the LOG."""
    health.record(1, stderr)
    assert health.snapshot().unauthenticated is False
    assert health.allow_attempt() is True


def test_every_condition_is_in_the_exported_vocabulary():
    """`CONDITIONS` is what consumers assert coverage against, so a condition
    that classify_gh_failure can return but CONDITIONS omits is a silent gap."""
    returned = {
        ghc.classify_gh_failure(s)
        for s in (STDERR_401, STDERR_UNAUTH_403, STDERR_NOT_LOGGED_IN,
                  STDERR_UNREACHABLE, STDERR_AUTHED_403, STDERR_SECONDARY_403)
    }
    assert returned == set(ghc.CONDITIONS)


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


@pytest.mark.parametrize("stderr", [
    "fatal: unable to access 'https://github.com/o/r.git/': "
    "Could not resolve host: github.com",
    "fatal: unable to access 'https://github.com/o/r.git/': "
    "Failed to connect to github.com port 443 after 21 ms: Connection refused",
    "fatal: unable to access 'https://github.com/o/r.git/': "
    "Failed to connect to github.com port 443: Connection timed out",
    "fatal: unable to access 'https://github.com/o/r.git/': Network is unreachable",
])
def test_classifies_gits_unreachable_phrasings(stderr):
    """git over libcurl words a dead network differently from gh (#2610)."""
    assert ghc.classify_gh_failure(stderr) == ghc.UNREACHABLE
