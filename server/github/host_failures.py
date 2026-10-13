"""One record for a gh failure that belongs to the HOST, not to its target.

Every gh-backed loop reports a failed call the same way: name the thing it was
reaching for, then quote gh's stderr. That is the right record when the *target*
is the problem, and precisely the wrong one when the host is — and the host is
the problem far more often, because a locked keyring, a dead network or an
exhausted budget breaks every loop at once.

:func:`server.obs.error_signature.signature_of` fingerprints a record on
``(source, level, logger, event-or-normalized-message, exc_class, frame)`` and
:mod:`server.obs.errsink` files one auto-error issue per fingerprint. So on
2026-09-17 one network outage arrived as five issues:

* #1922 — ``ci_issues``, on ``repo=steiner385/viberpit``
* #1923 — ``deployments``, on a **four**-repo list
* #1924 — ``deployments``, on a **five**-repo list
* #1926 — ``pr_poller``, on ``owner=cairnea``
* #1927 — ``pr_poller``, on ``owner=steiner385``

#1923/#1924 are the instructive pair. ``normalize()`` rewrites any quoted run to
``<str>``, so a list of repo *names* already collapsed — and the issues still
split, because ``repos=[<str>, <str>, <str>, <str>]`` and the five-element
version are different strings. Quoting the volatile value is not enough when its
*arity* is volatile too.

Collapsing to one issue therefore needs three constants at once, which is what
this module supplies:

* **one logger.** ``log`` here is a module-level name of its own rather than the
  caller's, because the logger is part of the fingerprint. Routing the record
  through each loop's logger keeps a lineage per loop — the reason #1922 stayed
  separate from #1926 even after #2117 had collapsed ci_issues' own targets.
  Note the throttle *hides* this: only one record escapes per window, so a
  per-caller logger looks collapsed within one outage and merely files under
  whichever loop lost the race, differently on each restart.
* **one frame.** ``emit_event`` is where ``logger.log`` is actually called, so
  ``module:funcName`` is constant for every caller by construction.
* **one message.** The condition is a structured ``event=`` name from a closed
  vocabulary; the volatile parts ride in ``fields``, which feed the issue title
  but not its identity.

The per-target detail is demoted to DEBUG, never dropped: someone who turned on
debug logging to watch an outage still needs to know which calls were lost.

Conditions come from ``gh_credential.classify_gh_failure`` in the CLI core, the
one place that reads gh stderr and names a host condition. Adding a fourth means
adding it there *and* an entry in :data:`_EVENTS` here; a condition with no entry
degrades to the old per-target WARNING rather than being filed under a
neighbouring condition's name, because folding unrelated conditions into one
event is the mirror-image defect (#1857) and costs more than a fan-out does.
``test_gh_host_failures.py`` fails on a missing entry, which is where that gap
should surface — not from a KeyError raised inside an error path.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from viberpit import gh_credential
from server.config import env_float
from server.obs.context import emit_event
from server.obs.logdedupe import LogThrottle, log_event_throttled

#: Not ``__name__``: the record is about the host, so it must read as the host's
#: in the journal *and* fingerprint as one thing no matter which loop noticed.
log = logging.getLogger("server.github.host")

#: condition -> (event name, the fields that explain what to do about it).
#:
#: One event per condition and no finer, per the rule that cost the #1857 family
#: nine days: the condition belongs in the NAME (so unlocking a keyring and
#: waiting out a rate limit are separate issues), while how badly one occurrence
#: hurt belongs in the level (so it does not split further).
_EVENTS: dict[str, tuple[str, dict[str, Any]]] = {
    gh_credential.UNAUTHENTICATED: (
        "github.host_gh_unauthenticated",
        {"runbook": "docs/runbooks/gh-credential-without-a-keyring.md"},
    ),
    gh_credential.UNREACHABLE: (
        "github.host_gh_unreachable",
        {"hint": "this host cannot reach api.github.com — check its network,"
                 " then https://www.githubstatus.com"},
    ),
    gh_credential.RATE_LIMITED: (
        "github.host_gh_rate_limited",
        {"hint": "this host's gh token is over its GitHub API budget; the loops"
                 " recover when the window resets"},
    ),
}

#: One window per *condition*, not per caller: while the host cannot reach
#: GitHub, a second loop noticing the same outage is not news. A newly *distinct*
#: condition still reports promptly because it is a different key.
#:
#: One instance rather than one per condition — these are one record family, and
#: at three keys against :data:`~server.obs.logdedupe.DEFAULT_CAPACITY` eviction
#: cannot happen. Every key reports at WARNING, so the "never share a key across
#: two levels" trap does not apply.
_HOST_THROTTLE = LogThrottle()

#: How long the host must keep failing to reach GitHub before the record is a
#: WARNING (= an auto-error issue). A blip of seconds recovers on its own and is
#: not actionable; only an outage that outlasts this is (#2742).
_UNREACHABLE_WARN_AFTER_ENV = "VIBERPIT_GH_UNREACHABLE_WARN_AFTER_S"
_UNREACHABLE_WARN_AFTER_DEFAULT_S = 300.0

#: Injectable for tests.
_clock = time.monotonic

_unreachable_lock = threading.Lock()
_unreachable_since: float | None = None


def _unreachable_outlasted_blip() -> bool:
    """Note one UNREACHABLE failure; True once the episode has lasted long enough
    to be worth a WARNING. The episode starts at the first failure after a
    success (see :func:`_note_reachable`)."""
    global _unreachable_since
    now = _clock()
    with _unreachable_lock:
        if _unreachable_since is None:
            _unreachable_since = now
        since = _unreachable_since
    return now - since >= env_float(
        _UNREACHABLE_WARN_AFTER_ENV, _UNREACHABLE_WARN_AFTER_DEFAULT_S, minimum=0.0
    )


def _note_reachable() -> None:
    global _unreachable_since
    with _unreachable_lock:
        _unreachable_since = None


gh_credential.on_success(_note_reachable)

#: gh's wording when GitHub itself failed a request that DID reach it — a
#: 502/503/504. Deliberately NOT one of the conditions above: those three are
#: correlated across every repo a host touches (one keyring, one route, one
#: budget), so collapsing them into a single issue is the fix. A 5xx from
#: GitHub's own edge is neither the host's fault nor reliably correlated with
#: any other repo's call in the same tick — ``test_a_failure_about_the_target_
#: still_names_the_target`` pins that a bare 502 must keep naming its target,
#: not fold into a host record.
#:
#: What it still needs is the read #1839 gave the CI/CD Doctor's artifact-zip
#: fetch: this is GitHub's outage, not ours, so it does not belong in the error
#: sink at all. ``pr_population``'s refresh loop hit the WARNING-per-tick
#: version of the same bug (#1681) — a poller that reaches GitHub every few
#: seconds turned one flaky upstream request into 600+ occurrences in a day.
#: Callers on a tight poll should check this and log at INFO instead of calling
#: :func:`log_gh_failure`, which is right for a failure that will not resolve
#: itself by simply trying again next tick.
_RE_TRANSIENT_5XX = re.compile(r"\bHTTP 5\d\d\b")

#: gh's Go JSON decoder's wording when GitHub's edge cut a response short
#: before it finished — the request reached GitHub, but the body never
#: completed. Same underlying condition as :data:`_RE_TRANSIENT_5XX` (a
#: heavy/slow query hitting GitHub's edge timeout), just a different gh error
#: string: ``encoding/json`` reports the truncation as "unexpected end of JSON
#: input" rather than a ``gh: ... (HTTP 5xx)``-style message, so it slipped
#: past every call site that only checked :func:`is_transient_5xx` (#2490).
_RE_TRUNCATED_JSON = re.compile(r"unexpected end of JSON input")

#: gh's Go HTTP/2 client's wording when GitHub's edge reset the stream
#: mid-request — e.g. ``stream error: stream ID 1; CANCEL; received from peer``.
#: The third wording of the same heavy-query edge timeout (#2677, after #1681's
#: 5xx and #2490's truncated JSON). ``received from peer`` is required: it is
#: what says GitHub sent the RST_STREAM, not our own client giving up.
_RE_STREAM_RESET = re.compile(
    r"stream error: stream ID \d+; (?:CANCEL|INTERNAL_ERROR|REFUSED_STREAM);"
    r" received from peer"
)

#: GitHub's generic GraphQL internal-error envelope: ``gh: Something went wrong
#: while executing your query on <ts>. Please include `<request-id>` when
#: reporting this issue.`` The fourth wording of the same GitHub-side transient
#: (#3025, after #1681/#2490/#2677). Anchored on the full "while executing your
#: query" phrase — not a bare "Something went wrong" — so it cannot mask an
#: unrelated defect; the timestamp and request id are deliberately not matched.
_RE_GRAPHQL_INTERNAL_ERROR = re.compile(r"Something went wrong while executing your query")


def is_transient_5xx(stderr: str) -> bool:
    """Whether *stderr* is gh reporting a GitHub-side 5xx: the request reached
    GitHub and GitHub's own edge failed it. Not a host condition (see
    :data:`_RE_TRANSIENT_5XX`) — just a shared read of when a per-target
    failure is GitHub's outage rather than a defect to chase."""
    return bool(_RE_TRANSIENT_5XX.search(stderr))


def is_transient_github_edge_failure(stderr: str) -> bool:
    """Whether *stderr* is GitHub's own edge failing a request that reached
    it — a bare 5xx (:func:`is_transient_5xx`), or a response cut short by the
    same edge timeout, either mid-body (:data:`_RE_TRUNCATED_JSON`) or as an
    HTTP/2 stream reset (:data:`_RE_STREAM_RESET`), or GraphQL's generic
    internal-error envelope (:data:`_RE_GRAPHQL_INTERNAL_ERROR`). Not a host condition — just
    the shared read of when a per-target failure is GitHub's outage rather than
    a defect to chase."""
    return (
        is_transient_5xx(stderr)
        or bool(_RE_TRUNCATED_JSON.search(stderr))
        or bool(_RE_STREAM_RESET.search(stderr))
        or bool(_RE_GRAPHQL_INTERNAL_ERROR.search(stderr))
    )


def log_gh_failure(
    caller_log: logging.Logger,
    stderr: str,
    msg: str,
    *args: object,
    exc_info: bool | BaseException | None = None,
    demote_edge_failures: bool = False,
) -> None:
    """Report one failed gh call — as the host condition when it is one.

    *caller_log*, *msg* and *args* are the record the call site would have
    emitted, unchanged. Pass the format string **exactly** as it was: for a
    failure that is genuinely about the target, this is still a plain
    ``caller_log.warning(msg, *args)``, and the sink fingerprints those on the
    normalized message, so rewording one forks its existing auto-error issue into
    a fresh lineage.

    ``exc_info`` rides the per-target record only. It is deliberately kept off
    the collapsed event: the last two fingerprint components are the exception
    class and the innermost app frame, so attaching a traceback would re-split
    the condition by httpx/subprocess failure mode — the 60-line stack was never
    the signal for a host that is simply offline.

    ``demote_edge_failures`` logs a GitHub-edge 5xx / truncated body / stream
    reset (:func:`is_transient_github_edge_failure`) at INFO instead.
    """
    condition = gh_credential.classify_gh_failure(stderr)
    entry = _EVENTS.get(condition) if condition is not None else None
    if (demote_edge_failures and condition is None
            and is_transient_github_edge_failure(stderr)):
        # Opt-in: for a heavy query where GitHub's own edge timing out (5xx,
        # truncated body, stream reset) is an outage to retry next tick, not a
        # defect to file. Default off — other callers treat a 5xx as the
        # target's problem and keep WARNING.
        caller_log.info(msg, *args, exc_info=exc_info)
        return
    if condition is None or entry is None:
        caller_log.warning(msg, *args, exc_info=exc_info)
        return
    event, fields = entry
    caller_log.debug(msg, *args, exc_info=exc_info)
    if condition == gh_credential.UNREACHABLE and not _unreachable_outlasted_blip():
        # Same event, logger and frame — only the level differs, so the
        # fingerprint is unchanged once the outage does outlast the blip window.
        emit_event(log, logging.INFO, event, fields)
        return
    log_event_throttled(
        log, _HOST_THROTTLE, condition, logging.WARNING, event, **fields,
    )
