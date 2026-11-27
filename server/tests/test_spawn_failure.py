"""spawn_failure.classify — the typed reason a queued session failed to start (#947)."""
from __future__ import annotations

import pytest

from server.sessions import preflight, spawn_failure


class _Sandbox(Exception):
    pass


class _Timeout(TimeoutError):
    pass


def _classify(exc, **kw):
    return spawn_failure.classify(exc, sandbox_error=_Sandbox, start_timeout=_Timeout, **kw)


@pytest.mark.parametrize("exc, reason, transient", [
    (RuntimeError("fatal: unable to access 'https://github.com/o/r.git/': "
                  "Could not resolve host: github.com"), "network", True),
    (_Timeout("driver start did not finish within 90s"), "start_timeout", True),
    (_Sandbox("bwrap missing"), "sandbox_refused", False),
    (RuntimeError("API Error: 401 invalid authentication credentials"), "auth", False),
    (RuntimeError("API Error: 429 rate_limit_error"), "rate_limited", False),
    (RuntimeError("insufficient disk headroom at /x"), "start_error", False),
])
def test_classify_table(exc, reason, transient):
    failure = _classify(exc)
    assert (failure.reason, failure.transient) == (reason, transient)


def test_obsolete_wins_over_the_exception():
    failure = _classify(RuntimeError("fatal: invalid reference: feat/x"), obsolete_pr=12)
    assert (failure.reason, failure.transient, failure.message) == (
        "obsolete", False, "PR #12 merged before start")


def test_message_is_first_line_capped():
    failure = _classify(RuntimeError("first line\n" + "trace " * 200))
    assert failure.message == "first line"
    long = _classify(RuntimeError("x" * 1000))
    assert len(long.message) == 300


def test_empty_message_falls_back_to_type_name():
    assert _classify(ValueError()).message == "ValueError"


def test_to_detail_shape():
    detail = _classify(RuntimeError("boom")).to_detail(2, requeued=False)
    assert detail == {"reason": "start_error", "transient": False, "attempt": 2,
                      "requeued": False, "message": "boom"}


# --- credential preflight (FR-304) ------------------------------------------------

@pytest.mark.parametrize("reason", sorted(preflight.PREFLIGHT_REASONS))
def test_preflight_failure_is_transient_when_unpinned(reason):
    """Rotation IS the retry: the drain re-enqueues once with the account excluded."""
    failure = _classify(preflight.PreflightFailed("alpha", reason))
    assert (failure.reason, failure.transient) == (reason, True)
    assert "alpha" in failure.message


def test_preflight_failure_is_permanent_when_pinned():
    """The user chose that account; silently moving them is wrong, so a pinned
    entry ends with the typed reason instead of rotating."""
    failure = _classify(preflight.PreflightFailed("alpha", "credentials_expired"), pinned=True)
    assert (failure.reason, failure.transient) == ("credentials_expired", False)


def test_pinned_does_not_change_other_rows():
    assert _classify(_Timeout("slow"), pinned=True).transient is True
    assert _classify(RuntimeError("Could not resolve host: github.com"),
                     pinned=True).transient is True


def test_preflight_row_sits_after_sandbox_before_timeout():
    class _Both(preflight.PreflightFailed, _Sandbox):
        pass
    assert _classify(_Both("a", "missing_credentials")).reason == "sandbox_refused"


# --- which account the failed spawn ran on ---------------------------------------

def test_note_and_read_failed_account():
    exc = RuntimeError("x")
    assert spawn_failure.failed_account(exc) is None
    spawn_failure.note_account(exc, "alpha")
    assert spawn_failure.failed_account(exc) == "alpha"
    spawn_failure.note_account(exc, "beta")         # first stamp wins
    assert spawn_failure.failed_account(exc) == "alpha"
    other = RuntimeError("y")
    spawn_failure.note_account(other, None)         # no account picked yet: no stamp
    assert spawn_failure.failed_account(other) is None


@pytest.mark.parametrize("exc, rotates", [
    (_Timeout("slow"), True),
    (preflight.PreflightFailed("alpha", "credentials_expired"), True),
    (preflight.PreflightFailed("alpha", "missing_credentials"), True),
    (RuntimeError("Could not resolve host: github.com"), False),   # network: same account
    (RuntimeError("insufficient disk headroom"), False),
])
def test_rotation_source_is_the_stamped_account_for_rotating_reasons(exc, rotates):
    spawn_failure.note_account(exc, "alpha")
    failure = _classify(exc)
    assert spawn_failure.rotation_source(exc, failure) == ("alpha" if rotates else None)


def test_rotation_source_needs_a_stamp():
    exc = _Timeout("slow")
    assert spawn_failure.rotation_source(exc, _classify(exc)) is None
