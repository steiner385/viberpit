"""Centralized redaction for the Codex app-server boundary.

This module is the **only** place `server/codex/` decides what may reach a log,
an exception message, a `repr`, or a support bundle. Native strings are opaque:
they may contain account identifiers, prompts, commands, paths, or model output
without looking like credentials, so the general scrubbers replace them
wholesale. Diagnostic call sites retain usefulness by logging only fixed
categories, reason codes, supported method names, and numeric protocol codes.

Credential-shape detection remains separate. It delegates to
`server.audit.redact_text` (the canonical application redactor) and adds only the
two shapes introduced at the Codex boundary:

* bare `sk-`-prefixed OpenAI keys (the shared list covers `sk-ant-` only), and
* short JWTs whose segments fall under the shared 3-part cookie pattern's
  minimum segment lengths.

Everything else — `sk-ant-*`, `ccenc1:*`, padded base64 blobs, long signed
triples — is already handled upstream, so there is exactly one owner per shape.

`Redacted` is the carrier for values that must never render: its `__repr__`,
`__str__`, and `__format__` all return `[REDACTED]`, so a dataclass holding one
is safe under the default dataclass `repr` and under `logging`'s `%s`.

`scrub_text` is therefore the safe primitive for Task 2.2 stderr: it never
attempts to infer which portions of native text are harmless.
"""
from __future__ import annotations

import re
from typing import Any, Final

from server.audit import redact_text as _shared_redact_text

REDACTED: Final = "[REDACTED]"

# Shapes the shared server.audit patterns do not fully cover. Keep this list
# minimal — a shape that belongs to the whole application belongs in server/audit.py.
_CODEX_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # OpenAI-style keys: sk-..., sk-proj-..., sk-svcacct-... (sk-ant-* is shared).
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    # JWT: three base64url segments starting with the `eyJ` header marker. The
    # shared itsdangerous pattern needs 16/6/20-char segments; access tokens with
    # a short payload or signature segment would slip past it.
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
)

class Redacted:
    """An opaque holder whose value never renders in logs, reprs, or f-strings."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the raw value. Call this only at a serialization boundary."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Redacted):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash((Redacted, self._value))


def scrub_text(text: str) -> str:
    """Replace arbitrary native diagnostic text without inspecting its content."""
    return REDACTED


def scrub_value(value: Any) -> str:
    """Replace an arbitrary native value, including its peer-controlled keys."""
    return REDACTED


def _scrub_secret_shapes(text: str) -> str:
    scrubbed = _shared_redact_text(text)
    for pattern in _CODEX_PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def contains_secret_shaped_value(text: str) -> bool:
    """Report whether text carries anything the redactors would have to remove."""
    return _scrub_secret_shapes(text) != text


def scrub_exception(exc: BaseException) -> str:
    """Reduce an exception to a fixed diagnostic category, never its message."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "transport_error"
    if isinstance(exc, ValueError):
        return "validation_error"
    if isinstance(exc, RuntimeError):
        return "runtime_error"
    return "internal_error"
