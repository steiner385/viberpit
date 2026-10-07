"""Classify a turn failure into a recovery kind.

A live session turn can fail with an HTTP 429 (rate-limited), 401 (invalid
credentials), or 403 (the account's organization has turned Claude Code off).
The orchestrator recovers those — same-account retry/recompose then rotation
for 429/401, straight to rotation for 403 (nothing to recompose). Everything
else keeps today's behavior (surface the error, mark the turn failed, no
rotation).

The classifier is pure and type-agnostic: it works off the ErrorEvent ``code``
and ``message`` the translator already produces (``api_429`` / ``api_401`` from
``ResultMessage.api_error_status``) AND off a raw exception string from the
driver's drain fallback, so it is robust to whichever shape the SDK surfaces.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

# Mirror oauth._RETRY_AFTER_CAP: never trust an absurd server-supplied delay.
_RETRY_AFTER_CAP = 120.0

# ``stream_lost`` is the driver's own verdict, never classified from text: the SDK
# cursor ended while the session was IDLE (the CLI exited, or its stdout reader died
# between turns). Recovered like a crash — recompose in place — but with no cut-off
# turn to replay. Mid-turn, the same loss is reported as ``process_crash``.
#
# ``auth_forbidden`` joined this set in #2244: unlike a 401 there is nothing to
# recompose or retry, but there IS a recovery — rotate to another account, the same
# way a 401 that survives recompose does. Only the "try again on this account"
# step is skipped (see orchestrator._recover_session).
#
# ``auth_stale`` — the CLI answered the prompt ITSELF with a ``<synthetic>``
# "Not logged in · Please run /login" and never reached the API. Seen 2026-10-07
# (session 1c1a0dfb): a long-lived CLI kept a stale OAuth token in memory after the
# slot was refreshed on disk, while every other session on the account worked. A
# relaunch re-reads the current credentials; see orchestrator._recover_auth_stale.
_RECOVERABLE = frozenset({"rate_limit", "auth_401", "auth_forbidden", "auth_stale",
                          "process_crash", "turn_dropped", "stream_lost"})

_OVERLOAD_STATUSES = frozenset({500, 502, 503, 529})
_OVERLOAD_CODES = frozenset(f"api_{s}" for s in _OVERLOAD_STATUSES)

# ``auth_forbidden`` — HTTP 403: the credential is valid but its *organization*
# has turned Claude Code access off. There is nothing to recompose or retry on
# THIS account, so live-turn handling (orchestrator._recover_session) rotates
# straight to another account rather than the recompose-then-rotate ladder a 401
# gets. Helper calls act on the same condition their own way — they pick their
# account, so they fail over to another one (helper_call._note_auth_blocked).
# Markers are the CLI's own spellings, seen in a helper transcript on 2026-09-22
# (#1820): apiErrorCode ``oauth_not_allowed_for_organization``, the assistant
# message's ``error`` field ``oauth_org_not_allowed``, and the sentence shown to
# the user. A bare "403" in prose is NOT a marker — an unrelated number must
# never cost an account its helper duty.
_NOT_LOGGED_IN_MARKERS = ("authentication_failed", "not logged in")

_FORBIDDEN_MARKERS = (
    "oauth_not_allowed_for_organization",
    "oauth_org_not_allowed",
    "organization has disabled claude",
    "ask your admin to enable access",
)

# ``auth_stale`` is matched on the CLI's own SENTENCE only (CLI 2.1.292, reproduced
# with an empty CLAUDE_CONFIG_DIR): "Not logged in · Please run /login", or
# "Invalid API key · Please run /login". Never on a fragment: every auth_stale
# costs relaunches and a spent cap rotates the session, so a loose "please run
# /login" or "authentication_failed" in arbitrary error text must not qualify. A
# session's own error result (``agent_error`` — the ResultMessage has no
# api_error_status) must BE the sentence; the helper and drain-fallback shapes
# wrap it ("... detail='authentication_failed | Not logged in · Please run
# /login'"), so for an uncoded or ``driver_error`` failure it may be embedded.
_AUTH_STALE_SENTENCE = re.compile(
    r"(?:not logged in|invalid api key) \u00b7 please run /login", re.IGNORECASE)
_AUTH_STALE_EMBEDDED_CODES = frozenset({"", "driver_error"})


def _is_auth_stale(code: str, text: str) -> bool:
    if code == "agent_error":
        return _AUTH_STALE_SENTENCE.fullmatch(text.strip()) is not None
    return code in _AUTH_STALE_EMBEDDED_CODES and _AUTH_STALE_SENTENCE.search(text) is not None


# ``network`` — the host could not reach the API at all: a dead resolver or an
# unreachable endpoint. Not recoverable by rotation (every account shares the
# host's network), but a short wait can clear it. Markers are Node's socket
# errno spellings plus the CLI's own sentence, seen during an 11h EAI_AGAIN
# outage on dobby (#2623): "API Error: Can't reach the API server — check your
# internet or DNS (EAI_AGAIN)".
_NETWORK_MARKERS = (
    "eai_again",
    "enotfound",
    "econnrefused",
    "econnreset",
    "etimedout",
    "can't reach the api server",
)

# ``cli_startup_timeout`` — the spawned CLI did not answer the SDK's
# ``initialize`` control handshake inside its fixed 60s window: a slow cold start
# on a loaded host, not a defect (#2664). Only ``initialize`` matches — a timed-out
# ``interrupt`` is a wedged live process and keeps surfacing as ``other``.
_STARTUP_TIMEOUT_MARKERS = ("control request timeout: initialize",)

# Kinds that are an external condition, never a defect of ours: logged below
# WARNING, because WARNING is the auto-error filing decision and filing an issue
# per throttle (#1688, #1837), DNS outage (#2623) or slow CLI start (#2664) files
# noise. ``auth_stale`` is deliberately NOT here: a helper spawns a fresh CLI, so
# for a helper "Not logged in" means the host's slot really is unusable (#2758 —
# the same 2026-10-07 incident, seen by goal inference first) and stays a filed
# WARNING. Live sessions do not read this; they journal their own recovery.
EXTERNAL_CONDITION_KINDS = frozenset({"rate_limit", "network", "cli_startup_timeout"})


@dataclass(frozen=True)
class TurnFailure:
    kind: str  # "rate_limit" | "auth_401" | "auth_forbidden" | "auth_stale" | "process_crash" | "stream_lost" | "turn_dropped" | "overloaded" | "network" | "cli_startup_timeout" | "other"
    retry_after: float | None = None

    @property
    def is_recoverable(self) -> bool:
        return self.kind in _RECOVERABLE


def log_level(kind: str | None) -> int:
    """The level to log a failure of *kind* at: INFO for an external condition,
    WARNING (an auto-filed error report) for everything else, unknown included."""
    return logging.INFO if kind in EXTERNAL_CONDITION_KINDS else logging.WARNING


def _cap(value: float | None) -> float | None:
    if value is None:
        return None
    if value < 0:
        return None
    return min(float(value), _RETRY_AFTER_CAP)


def _parse_retry_after(message: str) -> float | None:
    m = re.search(r"retry[-\s]?after[:\s]+(\d+(?:\.\d+)?)", message, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _is_process_crash(text: str) -> bool:
    low = text.lower()
    if "resource temporarily unavailable" in low or "errno 11" in low:
        return True
    if "terminated process" in low or "failed to start claude" in low:
        return True
    # negative/signal exit codes (-2..-N) = killed by signal (SIGABRT/SIGTRAP/…)
    if re.search(r"exit code:?\s*-\d+", low):
        return True
    # Positive exit codes >= 128 are the shell/CLI spelling of the same thing
    # (128+N = killed by signal N): 143=SIGTERM, 137=SIGKILL/OOM. A daemon restart
    # SIGTERMs live claude children, whose turns then surface "Command failed with
    # exit code 143" — an external process death, never a model/API failure
    # (2026-08-29: those turns were branded needs_attention across the restart).
    # Codes < 128 (a genuine CLI error exit) stay "other" and keep surfacing.
    m = re.search(r"exit code:?\s*(\d+)", low)
    if m and int(m.group(1)) >= 128:
        return True
    return False


def classify(code: str | None, message: str = "", *, retry_after: float | None = None) -> TurnFailure:
    """Map a turn failure to a ``TurnFailure``.

    ``code`` is the ErrorEvent code (``api_429`` / ``api_401`` / ``driver_error`` …);
    ``message`` is its human text or a raw exception string. ``retry_after`` may be
    supplied from a captured SDK RateLimitEvent reset; otherwise it is parsed from
    ``message`` when present. Unknown failures classify as ``other``.
    """
    code = code or ""
    text = message or ""
    low = text.lower()

    def rate_limit() -> TurnFailure:
        ra = retry_after if retry_after is not None else _parse_retry_after(text)
        return TurnFailure(kind="rate_limit", retry_after=_cap(ra))

    # An explicit ErrorEvent code is authoritative.
    if code == "api_429":
        return rate_limit()
    if code == "api_401":
        return TurnFailure(kind="auth_401")
    if code == "api_403":
        return TurnFailure(kind="auth_forbidden")
    if code in _OVERLOAD_CODES:
        return TurnFailure(kind="overloaded")

    # An organization-level denial, which the CLI may report with no status code
    # at all (the SDK's ProcessError replacement carries only the result subtype).
    if any(m in low for m in _FORBIDDEN_MARKERS):
        return TurnFailure(kind="auth_forbidden")

    # The CLI refused locally — no request was made, so nothing below applies.
    if _is_auth_stale(code, text):
        return TurnFailure(kind="auth_stale")

    # The CLI had no credential at all (#2758): its result subtype is
    # "authentication_failed" / "Not logged in" with no HTTP status to match.
    if any(m in low for m in _NOT_LOGGED_IN_MARKERS):
        return TurnFailure(kind="auth_401")

    # A turn that dropped before the model committed any continuation — the CLI tags it
    # "[ede_diagnostic] ... stop_reason=null". Distinct from the orphaned-tool_use variant
    # (stop_reason=tool_use), which transcript_repair heals on resume. Nothing was
    # committed, so re-sending the same prompt is idempotent and safe.
    if code == "execution_error" and "[ede_diagnostic]" in text and "stop_reason=null" in text:
        return TurnFailure(kind="turn_dropped")

    # Check for process crash signatures before general content inference.
    if _is_process_crash(text):
        return TurnFailure(kind="process_crash")

    # The request never reached the API, so no status-bearing content below applies.
    if any(m in low for m in _NETWORK_MARKERS):
        return TurnFailure(kind="network")

    if any(m in low for m in _STARTUP_TIMEOUT_MARKERS):
        return TurnFailure(kind="cli_startup_timeout")

    # Otherwise infer from content. Auth is checked first: an "invalid
    # authentication credentials" / 401 message is unambiguous, whereas a bare
    # "429" could co-occur. A real turn failure carries only one of these.
    is_429 = "429" in text or "rate_limit" in low or "rate limit" in low
    is_401 = "401" in text or "invalid authentication credentials" in low
    if is_401:
        return TurnFailure(kind="auth_401")
    if is_429:
        return rate_limit()
    is_overload = "overloaded" in low or any(str(s) in text for s in _OVERLOAD_STATUSES)
    if is_overload:
        return TurnFailure(kind="overloaded")
    return TurnFailure(kind="other")
