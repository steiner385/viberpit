"""Classify a turn failure into a recovery kind.

A live session turn can fail with an HTTP 429 (rate-limited) or 401 (invalid
credentials). The orchestrator recovers those — same-account retry/recompose,
then rotation. Everything else keeps today's behavior (surface the error, mark
the turn failed, no rotation).

The classifier is pure and type-agnostic: it works off the ErrorEvent ``code``
and ``message`` the translator already produces (``api_429`` / ``api_401`` from
``ResultMessage.api_error_status``) AND off a raw exception string from the
driver's drain fallback, so it is robust to whichever shape the SDK surfaces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Mirror oauth._RETRY_AFTER_CAP: never trust an absurd server-supplied delay.
_RETRY_AFTER_CAP = 120.0

_RECOVERABLE = frozenset({"rate_limit", "auth_401", "process_crash", "turn_dropped"})

_OVERLOAD_STATUSES = frozenset({500, 502, 503, 529})
_OVERLOAD_CODES = frozenset(f"api_{s}" for s in _OVERLOAD_STATUSES)


@dataclass(frozen=True)
class TurnFailure:
    kind: str  # "rate_limit" | "auth_401" | "process_crash" | "turn_dropped" | "overloaded" | "other"
    retry_after: float | None = None

    @property
    def is_recoverable(self) -> bool:
        return self.kind in _RECOVERABLE


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
    if code in _OVERLOAD_CODES:
        return TurnFailure(kind="overloaded")

    # A turn that dropped before the model committed any continuation — the CLI tags it
    # "[ede_diagnostic] ... stop_reason=null". Distinct from the orphaned-tool_use variant
    # (stop_reason=tool_use), which transcript_repair heals on resume. Nothing was
    # committed, so re-sending the same prompt is idempotent and safe.
    if code == "execution_error" and "[ede_diagnostic]" in text and "stop_reason=null" in text:
        return TurnFailure(kind="turn_dropped")

    # Check for process crash signatures before general content inference.
    if _is_process_crash(text):
        return TurnFailure(kind="process_crash")

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
