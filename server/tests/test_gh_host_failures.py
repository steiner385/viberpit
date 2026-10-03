"""One GitHub outage must file ONE auto-error issue (#1922-#1927, #1941).

On 2026-09-17 dobby lost its route to api.github.com for six minutes. ViberPit
filed **five** issues for it, and they differ only in what each loop happened to
be reaching for when it noticed:

    #1922  server.github.ci_issues     repo=steiner385/viberpit
    #1923  server.github.deployments   a four-repo list
    #1924  server.github.deployments   a five-repo list
    #1926  server.github.pr_poller     owner=cairnea
    #1927  server.github.pr_poller     owner=steiner385

#1941 is the same story for a second condition: gh rate-limited with no
credential, split off by the same per-target reporting.

#1928 is the sixth issue from that same six minutes, and the reason this file
sweeps a loop's *every* gh boundary rather than one per module: the population
poller reaches GitHub three times a cycle — the population query, the ETA query
and the merge-queue query — each behind its own ``except``, so one dead network
mints ``pr_population.refresh_failed``, ``pr_population.eta_fetch_failed`` and
``pr_population.merge_queue_failed``. Collapsing whichever one a report names
leaves the other two live, which is how #1928 outlived the collapse (#2117) that
had already fixed its siblings.

The assertions below therefore go through ``signature_of`` rather than counting
log lines. Counting WARNINGs would pass on a change that collapsed the *records*
while still minting a fingerprint per logger, which is exactly how #1922 stayed
separate from #1926 after the per-target collapse already existed (#2117): the
logger is part of the key, so a shared helper that logs on the caller's logger
fixes nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess

import pytest

from server.github import (
    actions_api,
    ci_issues,
    deployments,
    host_failures,
    pr_poller,
    pr_population,
)
from server.automations import archive_audit
from server.obs.error_signature import signature_of
from server.sessions import dispatch

from .test_population_eta_wired import (
    REPO as POP_REPO,
    _eta_checks_resp_empty,
    _pop_resp,
)

#: The classifier, reached through the module that owns the collapse so the test
#: exercises the object the production path does.
ghc = host_failures.gh_credential

# Byte-for-byte from the issues named above.
STDERR_UNREACHABLE = (
    "error connecting to api.github.com\n"
    "check your internet connection or https://githubstatus.com"
)
# Byte-for-byte from #1936: gh's Go transport error when the TCP connection
# opened but the TLS handshake never completed — the request never reached GitHub.
STDERR_TLS_HANDSHAKE_TIMEOUT = (
    'Post "https://api.github.com/graphql": net/http: TLS handshake timeout'
)
STDERR_RATE_LIMITED_IP = (
    "gh: API rate limit exceeded for 184.58.36.32. (But here's the good news: "
    "Authenticated requests get a higher rate limit. Check out the documentation "
    "for more details.) (HTTP 403)"
)
STDERR_RATE_LIMITED_USER = "gh: API rate limit exceeded for user ID 1234567. (HTTP 403)"
STDERR_UNRELATED = "gh: Bad Gateway (HTTP 502)"
# Neither a host condition nor GitHub's transient edge: pr_poller demotes a 5xx
# to INFO (#2669), so a lineage test driven through it needs a non-5xx failure.
STDERR_UNRELATED_NOT_5XX = "gh: Resource not accessible by integration (HTTP 403)"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """The collapse window is module state; each test starts at first sighting."""
    host_failures._HOST_THROTTLE.clear()
    yield
    host_failures._HOST_THROTTLE.clear()


def _runner(stderr: str, *, rc: int = 1, stdout: str = ""):
    """A gh that always fails *stderr*. Accepts every kwarg the call sites pass."""
    def run(args, *a, **k):
        return subprocess.CompletedProcess(args, rc, stdout, stderr)
    return run


# --------------------------------------------------------------------------
# the population poller's three gh boundaries (#1928)
# --------------------------------------------------------------------------
# One refresh cycle reaches GitHub three times, each behind its own ``except``.
# Each drive below lets the OTHER two succeed, so a regression names the
# boundary that regressed instead of failing as one undifferentiated blob —
# and so a fix to one of them cannot make the sweep look green for all three.

async def _noop_mq_sink(_by_repo, _captured_at) -> None:
    return None


async def _instant_sleep(_secs: float) -> None:
    return None


def _drive_population_query() -> None:
    """Boundary 1 — the population query (``_default_query``)."""
    asyncio.run(pr_population.PrPopulationPoller().refresh_once([POP_REPO]))


def _drive_population_eta() -> None:
    """Boundary 2 — the ETA query. The jittered 5–30 s retry is real behaviour
    (#671) and is zeroed rather than bypassed: the retry still runs, it just
    does not make this test sleep for half a minute."""
    poller = pr_population.PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_retry_min_s=0.0,
        eta_retry_max_s=0.0,
        sleep_fn=_instant_sleep,
    )
    asyncio.run(poller.refresh_once([POP_REPO]))


def _drive_population_merge_queue() -> None:
    """Boundary 3 — the merge-queue query, the one #1928 was filed against."""
    poller = pr_population.PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=_eta_checks_resp_empty,
        merge_queue_query_fn=pr_population._default_merge_queue_query,
        merge_queue_sink=_noop_mq_sink,
    )
    asyncio.run(poller.refresh_once([POP_REPO]))


# --------------------------------------------------------------------------
# every loop, one fingerprint
# --------------------------------------------------------------------------

#: The gh-backed loops the 2026-09-17 outage hit, each driven through its REAL
#: entry point rather than a hand-built log record: the thing under test is what
#: the production call sites do with a failed gh call.
LOOPS: list[tuple[str, object]] = [
    ("ci_issues.issue_states",
     lambda run: ci_issues.issue_states("steiner385/viberpit", [1713], run=run)),
    ("ci_issues.comment_on_issue",
     lambda run: ci_issues.comment_on_issue("cairnea/KinDash", 42, "x", run=run)),
    ("deployments(4 repos)",
     lambda run: deployments.fetch_deployments(
         ["steiner385/ghostdriver-mcp", "steiner385/lakota-grades-mcp",
          "steiner385/sort-it", "steiner385/viberpit"], run=run)),
    ("deployments(5 repos)",
     lambda run: deployments.fetch_deployments(
         ["cairnea/KinDash", "cairnea/icon-pack", "cairnea/kwgt-cairn",
          "cairnea/plans", "cairnea/web"], run=run)),
    ("pr_poller(steiner385)",
     lambda run: pr_poller._default_query([("steiner385/viberpit", "master")])),
    ("pr_poller(cairnea)",
     lambda run: pr_poller._default_query([("cairnea/KinDash", "main")])),
    ("dispatch.fetch_open_issues",
     lambda run: dispatch.fetch_open_issues("steiner385/viberpit", run_gh=run)),
    ("actions_api.repo_ci_meta",
     lambda run: actions_api.repo_ci_meta("steiner385/viberpit", run=run)),
    ("pr_population(population query)",
     lambda run: _drive_population_query()),
    ("pr_population(eta query)",
     lambda run: _drive_population_eta()),
    ("pr_population(merge queue)",
     lambda run: _drive_population_merge_queue()),
]


def _patch_module_level_runners(run, monkeypatch) -> None:
    """pr_poller and pr_population reach gh through the module rather than an
    injected runner. ``token_for_owner`` is stubbed too: minting a real App
    installation token is itself a gh call, and it would fail first and mask
    the boundary under test."""
    monkeypatch.setattr(pr_poller.gitrepos, "_run_gh", run)
    monkeypatch.setattr(pr_poller.gitrepos, "app_token_for_owner", lambda owner: None)
    monkeypatch.setattr(pr_population.gitrepos, "_run_gh", run)
    monkeypatch.setattr(pr_population.app_auth, "token_for_owner", lambda owner: None)


def _drive_every_loop(stderr: str, monkeypatch) -> None:
    run = _runner(stderr)
    _patch_module_level_runners(run, monkeypatch)
    for _name, drive in LOOPS:
        drive(run)  # type: ignore[operator]


def _fingerprints(caplog) -> set[str]:
    out = set()
    for rec in caplog.records:
        if rec.levelno < logging.WARNING:
            continue
        sig = signature_of(rec, source="daemon", host="dobby")
        if sig is not None:
            out.add(sig.fingerprint)
    return out


def _loggers_exercised(caplog) -> set[str]:
    return {r.name for r in caplog.records}


@pytest.mark.parametrize("stderr,expected_event", [
    (STDERR_UNREACHABLE, "github.host_gh_unreachable"),
    (STDERR_TLS_HANDSHAKE_TIMEOUT, "github.host_gh_unreachable"),
    (STDERR_RATE_LIMITED_IP, "github.host_gh_unauthenticated"),
    (STDERR_RATE_LIMITED_USER, "github.host_gh_rate_limited"),
])
def test_one_host_condition_files_one_issue(stderr, expected_event, caplog, monkeypatch):
    """The regression these six issues are: eight failing calls across five
    loggers, one fingerprint."""
    with caplog.at_level(logging.DEBUG):
        _drive_every_loop(stderr, monkeypatch)

    # Guard the guard: a sweep that exercised nothing would pass vacuously.
    assert len(_loggers_exercised(caplog)) >= 4, (
        f"only {_loggers_exercised(caplog)} logged — the loops did not run")

    fps = _fingerprints(caplog)
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(fps) == 1, (
        f"{len(fps)} issues would be filed for one condition: "
        f"{sorted({(r.name, r.getMessage()[:60]) for r in warned})}")
    assert {getattr(r, "event", None) for r in warned} == {expected_event}


def test_whichever_loop_notices_first_files_the_same_issue(caplog, monkeypatch):
    """The throttle alone is NOT the fix, and this is the assertion that says so.

    Only one record escapes per window, so within a single outage a per-caller
    logger already looks collapsed — it just files under whichever loop lost the
    race. Across process restarts that is a different loop each time, and the
    logger is part of the fingerprint, so the lineage forks silently over weeks.
    Each loop is therefore driven here as the first to notice in its OWN window,
    which is the only way the shared-logger decision is observable.
    """
    run = _runner(STDERR_UNREACHABLE)
    _patch_module_level_runners(run, monkeypatch)
    first_reporter: dict[str, set[str]] = {}
    for name, drive in LOOPS:
        host_failures._HOST_THROTTLE.clear()
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            drive(run)  # type: ignore[operator]
        first_reporter[name] = _fingerprints(caplog)

    assert all(fps for fps in first_reporter.values()), (
        f"a loop reported nothing at all: {first_reporter}")
    distinct = {fp for fps in first_reporter.values() for fp in fps}
    assert len(distinct) == 1, (
        f"{len(distinct)} lineages depending on which loop failed first: "
        f"{first_reporter}")


def test_the_three_conditions_stay_three_issues(caplog, monkeypatch):
    """The mirror-image defect (#1857): collapsing far enough to hide a second
    condition. Unlocking a keyring, waiting out a budget and fixing a network
    are three fixes and must stay three issues."""
    seen = {}
    for stderr in (STDERR_UNREACHABLE, STDERR_RATE_LIMITED_IP, STDERR_RATE_LIMITED_USER):
        host_failures._HOST_THROTTLE.clear()
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            _drive_every_loop(stderr, monkeypatch)
        seen[stderr] = _fingerprints(caplog)
    flat = [fp for fps in seen.values() for fp in fps]
    assert len(flat) == 3 and len(set(flat)) == 3, seen


def test_the_per_target_detail_survives_at_debug(caplog, monkeypatch):
    """Demoted, not dropped — whoever turned on debug logging to watch the
    outage still needs to know which calls were lost."""
    with caplog.at_level(logging.DEBUG):
        _drive_every_loop(STDERR_UNREACHABLE, monkeypatch)
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("steiner385/ghostdriver-mcp" in m for m in debug)
    assert any("pr_poller.query_failed" in m for m in debug)


def test_repeats_inside_the_window_are_demoted(caplog, monkeypatch):
    """The level SEQUENCE, not "a WARNING was emitted" — the latter passes
    identically with and without the throttle."""
    levels = []
    with caplog.at_level(logging.DEBUG, logger=host_failures.log.name):
        for _ in range(4):
            host_failures.log_gh_failure(
                logging.getLogger("t"), STDERR_UNREACHABLE, "x")
    levels = [r.levelname for r in caplog.records
              if r.name == host_failures.log.name]
    assert levels == ["WARNING", "DEBUG", "DEBUG", "DEBUG"]


# --------------------------------------------------------------------------
# the splitters the issues were actually made of
# --------------------------------------------------------------------------

def test_a_four_and_a_five_repo_group_are_one_lineage(caplog):
    """#1923 vs #1924, the subtle one. `normalize()` masks a quoted run, so a
    *list* of repos already collapsed its names — and still split, because
    `repos=[<str>, <str>, <str>, <str>]` is not the five-element string. Pinned
    on an UNRELATED failure on purpose: the classifier fix hides this one, and
    the next non-host failure would have re-split it."""
    run = _runner(STDERR_UNRELATED)
    with caplog.at_level(logging.DEBUG):
        deployments.fetch_deployments(["o/a", "o/b", "o/c", "o/d"], run=run)
        deployments.fetch_deployments(["o/a", "o/b", "o/c", "o/d", "o/e"], run=run)
    assert len(_fingerprints(caplog)) == 1


def test_two_owners_are_one_lineage(caplog, monkeypatch):
    """#1926 vs #1927 — same, split by a bare slug."""
    monkeypatch.setattr(pr_poller.gitrepos, "_run_gh", _runner(STDERR_UNRELATED_NOT_5XX))
    monkeypatch.setattr(pr_poller.gitrepos, "app_token_for_owner", lambda owner: None)
    with caplog.at_level(logging.DEBUG):
        pr_poller._default_query([("steiner385/viberpit", "master")])
        pr_poller._default_query([("cairnea/KinDash", "main")])
    assert len(_fingerprints(caplog)) == 1


# --------------------------------------------------------------------------
# what must NOT change
# --------------------------------------------------------------------------

def test_a_failure_about_the_target_still_names_the_target(caplog):
    """The collapse is for HOST conditions only. A 502 against one repo is that
    repo's problem and must keep reporting at WARNING with its detail."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.issue_states("o/r", [7], run=_runner(STDERR_UNRELATED))
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert warned[0].getMessage() == (
        "github.issue_states_failed repo=o/r: gh: Bad Gateway (HTTP 502)")


def test_a_new_condition_reports_promptly_while_another_is_latched(caplog):
    """Pacing one host condition must never gag a different one that arrives
    inside its window — they have different fixes."""
    t = logging.getLogger("t")
    with caplog.at_level(logging.DEBUG, logger=host_failures.log.name):
        host_failures.log_gh_failure(t, STDERR_UNREACHABLE, "x")
        host_failures.log_gh_failure(t, STDERR_RATE_LIMITED_USER, "x")
    warned = [r for r in caplog.records
              if r.levelno >= logging.WARNING and r.name == host_failures.log.name]
    assert [r.event for r in warned] == [  # type: ignore[attr-defined]
        "github.host_gh_unreachable", "github.host_gh_rate_limited"]


def test_the_traceback_never_rides_the_collapsed_event(caplog):
    """The last two fingerprint components are the exception class and the
    innermost app frame, so a traceback on the collapsed record would re-split
    one offline host by subprocess failure mode."""
    with caplog.at_level(logging.DEBUG, logger=host_failures.log.name):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            host_failures.log_gh_failure(
                logging.getLogger("t"), STDERR_UNREACHABLE, "x", exc_info=True)
    collapsed = [r for r in caplog.records if r.name == host_failures.log.name]
    assert collapsed and all(r.exc_info is None for r in collapsed)


def test_is_transient_5xx_matches_gh_server_errors():
    assert host_failures.is_transient_5xx("gh: HTTP 502") is True
    assert host_failures.is_transient_5xx("gh: Bad Gateway (HTTP 502)") is True
    assert host_failures.is_transient_5xx("gh: HTTP 503") is True
    assert host_failures.is_transient_5xx("gh: HTTP 504") is True
    assert host_failures.is_transient_5xx(STDERR_UNREACHABLE) is False
    assert host_failures.is_transient_5xx("gh: not found (HTTP 404)") is False
    assert host_failures.is_transient_5xx("") is False


def test_is_transient_github_edge_failure_also_matches_truncated_json():
    """#2490: a heavy query that GitHub's edge cuts short arrives as gh's Go
    JSON decoder complaining, not as an HTTP 5xx — same underlying condition,
    different wording, so it needs its own match alongside is_transient_5xx."""
    assert host_failures.is_transient_github_edge_failure(
        "unexpected end of JSON input") is True
    assert host_failures.is_transient_github_edge_failure("gh: HTTP 502") is True
    assert host_failures.is_transient_github_edge_failure(STDERR_UNREACHABLE) is False
    assert host_failures.is_transient_github_edge_failure(
        "gh: not found (HTTP 404)") is False
    assert host_failures.is_transient_github_edge_failure("") is False


def test_success_is_untouched():
    url = ci_issues.comment_on_issue(
        "o/r", 7, "hi",
        run=_runner("", rc=0, stdout=json.dumps({"html_url": "https://x/1"})))
    assert url == "https://x/1"


# --------------------------------------------------------------------------
# conformance
# --------------------------------------------------------------------------

def test_every_classified_condition_has_an_event():
    """A condition added to the classifier with no entry here degrades silently
    back to per-target reporting — the exact fan-out this module exists to stop.
    Asserted against the exported vocabulary so it cannot be satisfied by
    hardcoding today's three names on both sides."""
    assert set(host_failures._EVENTS) == set(ghc.CONDITIONS)


def test_each_event_name_is_distinct():
    events = [event for event, _ in host_failures._EVENTS.values()]
    assert len(set(events)) == len(events)


# --------------------------------------------------------------------------
# archive_audit's two gh boundaries (#2611)
# --------------------------------------------------------------------------

def _archive_audit_calls(stderr: str, monkeypatch) -> None:
    monkeypatch.setattr(archive_audit.gitrepos, "_run_gh", _runner(stderr))
    monkeypatch.setattr(archive_audit.gitrepos, "app_token_for_owner", lambda owner: None)
    archive_audit._real_gh("a/b", "x")
    archive_audit._real_gh("c/d", "y")
    archive_audit._real_gh_num("a/b", 1)
    archive_audit._real_gh_num("c/d", 2)


def test_archive_audit_host_outage_files_one_issue(caplog, monkeypatch):
    """One outage probed across different repos/branches/PR numbers was seven issues."""
    with caplog.at_level(logging.DEBUG):
        _archive_audit_calls(STDERR_UNREACHABLE, monkeypatch)
    assert len(_fingerprints(caplog)) == 1
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert {getattr(r, "event", None) for r in warned} == {"github.host_gh_unreachable"}


def test_archive_audit_target_failure_still_names_the_target(caplog, monkeypatch):
    with caplog.at_level(logging.DEBUG):
        _archive_audit_calls(STDERR_UNRELATED, monkeypatch)
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("repo=a/b head=x" in m for m in warned)
    assert any("repo=c/d number=2" in m for m in warned)
