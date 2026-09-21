"""ci_issues collapses a locked-keyring host into ONE signal, not one per target (#2052).

The incident: 18 `github.issue_comment_failed` / `github.issue_states_failed`
WARNINGs naming 18 unrelated issues (#1713, #1793, #1810, …), all of which were
open and fine. The defect was the host's gh credential; the per-target records
were noise that buried it — and each one had already spent a request from the
60/hr unauthenticated IP bucket.
"""
from __future__ import annotations

import json
import logging
import subprocess

import pytest

from server.github import ci_issues, host_failures

#: The classifier the module under test routes through; reached via the module
#: that owns the collapse so the test exercises the object the production path
#: does. Cross-loop behaviour lives in ``test_gh_host_failures.py``; this file
#: stays on ci_issues' four gh paths, the ones #2052 actually burned.
ghc = host_failures.gh_credential

STDERR_401 = "gh: Requires authentication (HTTP 401)"
STDERR_UNAUTH_403 = (
    "gh: API rate limit exceeded for 203.0.113.7. (But here's the good news: "
    "Authenticated requests get a higher rate limit.) (HTTP 403)"
)


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """The collapse window is module state; each test starts at first-sighting."""
    host_failures._HOST_THROTTLE.clear()
    yield
    host_failures._HOST_THROTTLE.clear()


def _runner(*, rc=1, stderr="", stdout=""):
    def run(args, *a, **k):
        return subprocess.CompletedProcess(args, rc, stdout, stderr)
    return run


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# --------------------------------------------------------------------------
# the collapse
# --------------------------------------------------------------------------

def test_a_401_comment_reports_the_host_not_the_issue(caplog):
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        assert ci_issues.comment_on_issue(
            "steiner385/viberpit", 1713, "x", run=_runner(stderr=STDERR_401)) is None
    warned = _warnings(caplog)
    assert len(warned) == 1
    msg = warned[0].getMessage()
    assert "host_gh_unauthenticated" in msg
    assert "1713" not in msg, "the target issue is not the defect and must not name it"


def test_the_per_target_detail_survives_at_debug(caplog):
    """Demoted, not dropped — someone who turned on debug logging to watch this
    still needs to know which calls were lost."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.comment_on_issue("o/r", 1713, "x", run=_runner(stderr=STDERR_401))
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("github.issue_comment_failed" in m and "1713" in m for m in debug)


def test_many_targets_collapse_to_one_record(caplog):
    """18 distinct issues, one condition, one WARNING."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        for number in (1713, 1793, 1810, 2002, 1942, 1643):
            ci_issues.comment_on_issue("o/r", number, "x", run=_runner(stderr=STDERR_401))
    assert len(_warnings(caplog)) == 1


def test_the_unauthenticated_403_on_reads_collapses_with_the_401(caplog):
    """The 401 wave and the IP-rate-limit 403 wave are ONE condition seen from the
    write side and the read side, and must not file as two."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.comment_on_issue("o/r", 1713, "x", run=_runner(stderr=STDERR_401))
        ci_issues.issue_states("o/r", [1713], run=_runner(stderr=STDERR_UNAUTH_403))
    assert len(_warnings(caplog)) == 1


def test_a_suppressed_call_collapses_too(caplog):
    """Once `_run_gh` starts backing off, its synthetic stderr must read as the
    same condition rather than as a new mystery failure."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.create_issue("o/r", "t", "b", labels=[],
                               run=_runner(stderr=ghc.SUPPRESSED_STDERR))
    warned = _warnings(caplog)
    assert len(warned) == 1 and "host_gh_unauthenticated" in warned[0].getMessage()


@pytest.mark.parametrize("call", [
    lambda run: ci_issues.create_issue("o/r", "t", "b", labels=[], run=run),
    lambda run: ci_issues.comment_on_pr("o/r", 7, "x", run=run),
    lambda run: ci_issues.comment_on_issue("o/r", 7, "x", run=run),
    lambda run: ci_issues.issue_states("o/r", [7], run=run),
])
def test_every_logging_gh_path_collapses(call, caplog):
    """A path that kept its own bare log.warning would keep its own issue lineage."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        call(_runner(stderr=STDERR_401))
    warned = _warnings(caplog)
    assert len(warned) == 1, f"expected one host record, got {[r.getMessage() for r in warned]}"
    assert "host_gh_unauthenticated" in warned[0].getMessage()


# --------------------------------------------------------------------------
# what must NOT change
# --------------------------------------------------------------------------

def test_an_unrelated_failure_keeps_its_exact_message(caplog):
    """The error sink fingerprints on the normalized message, so altering these
    strings would fork every existing auto-error lineage into a new issue."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.comment_on_issue("o/r", 7, "x",
                                   run=_runner(stderr="gh: Not Found (HTTP 404)"))
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert warned[0].getMessage() == (
        "github.issue_comment_failed repo=o/r #7: gh: Not Found (HTTP 404)")


def test_unrelated_issue_states_failure_keeps_its_exact_message(caplog):
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.issue_states("o/r", [7], run=_runner(stderr="gh: Bad Gateway (HTTP 502)"))
    assert _warnings(caplog)[0].getMessage() == (
        "github.issue_states_failed repo=o/r: gh: Bad Gateway (HTTP 502)")


def test_unrelated_create_and_pr_comment_messages_are_unchanged(caplog):
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.create_issue("o/r", "t", "b", labels=[],
                               run=_runner(stderr="gh: Validation Failed (HTTP 422)"))
        ci_issues.comment_on_pr("o/r", 9, "x",
                                run=_runner(stderr="gh: Validation Failed (HTTP 422)"))
    msgs = [r.getMessage() for r in _warnings(caplog)]
    assert "cicd.issue_create_failed repo=o/r: gh: Validation Failed (HTTP 422)" in msgs
    assert "github.pr_comment_failed repo=o/r #9: gh: Validation Failed (HTTP 422)" in msgs


def test_success_is_untouched():
    url = ci_issues.comment_on_issue(
        "o/r", 7, "hello", run=_runner(rc=0, stdout=json.dumps({"html_url": "https://x/1"})))
    assert url == "https://x/1"


def test_a_new_condition_reports_promptly_even_while_the_host_is_latched(caplog):
    """Pacing the host condition must not silence an unrelated failure that
    happens to arrive inside its window."""
    with caplog.at_level(logging.DEBUG, logger=ci_issues.log.name):
        ci_issues.comment_on_issue("o/r", 1713, "x", run=_runner(stderr=STDERR_401))
        ci_issues.comment_on_issue("o/r", 1713, "x",
                                   run=_runner(stderr="gh: Not Found (HTTP 404)"))
    msgs = [r.getMessage() for r in _warnings(caplog)]
    assert len(msgs) == 2
    assert any("Not Found" in m for m in msgs)
