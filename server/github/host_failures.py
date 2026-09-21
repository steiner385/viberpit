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

Conditions come from :func:`ccswitch.gh_credential.classify_gh_failure`, the one
place that reads gh stderr and names a host condition. Adding a fourth means
adding it there *and* an entry in :data:`_EVENTS` here; a condition with no entry
degrades to the old per-target WARNING rather than being filed under a
neighbouring condition's name, because folding unrelated conditions into one
event is the mirror-image defect (#1857) and costs more than a fan-out does.
``test_gh_host_failures.py`` fails on a missing entry, which is where that gap
should surface — not from a KeyError raised inside an error path.
"""
from __future__ import annotations

import logging
from typing import Any

from ccswitch import gh_credential
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


def log_gh_failure(
    caller_log: logging.Logger,
    stderr: str,
    msg: str,
    *args: object,
    exc_info: bool | BaseException | None = None,
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
    """
    condition = gh_credential.classify_gh_failure(stderr)
    entry = _EVENTS.get(condition) if condition is not None else None
    if condition is None or entry is None:
        caller_log.warning(msg, *args, exc_info=exc_info)
        return
    event, fields = entry
    caller_log.debug(msg, *args, exc_info=exc_info)
    log_event_throttled(
        log, _HOST_THROTTLE, condition, logging.WARNING, event, **fields,
    )
