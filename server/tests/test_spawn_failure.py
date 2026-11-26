"""spawn_failure.classify — the typed reason a queued session failed to start (#947)."""
from __future__ import annotations

import pytest

from server.sessions import spawn_failure


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
