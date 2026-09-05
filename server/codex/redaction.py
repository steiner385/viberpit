"""Centralized redaction for the Codex app-server boundary.

This module is the **only** place `server/codex/` decides what may reach a log,
an exception message, a `repr`, or a support bundle. Native strings are opaque:
they may contain account identifiers, prompts, commands, paths, or model output
without looking like credentials, so the general scrubbers replace them
wholesale. Diagnostic call sites retain usefulness by logging only fixed
categories, reason codes, supported method names, and numeric protocol codes.

Credential-shape detection is separate, and is NOT reimplemented here: it
delegates to `server.audit.redact_text`, the canonical application redactor, which
owns every shape — `sk-*` (Anthropic and OpenAI), JWTs, `ccenc1:*`, padded base64
blobs, long signed triples. `scrub_secret_shapes` is the narrow public entry point
for the rare value that must stay readable.

`Redacted` is the carrier for values that must never render: its `__repr__`,
`__str__`, and `__format__` all return `[REDACTED]`, so a dataclass holding one
is safe under the default dataclass `repr` and under `logging`'s `%s`.

`scrub_text` is therefore the safe primitive for Task 2.2 stderr: it never
attempts to infer which portions of native text are harmless.
"""
from __future__ import annotations

from typing import Any, Final

from server.audit import redact_text as _shared_redact_text

REDACTED: Final = "[REDACTED]"

# NOTE: this module no longer carries credential-shape patterns of its own.
#
# The bare `sk-` OpenAI key and the short-JWT shapes used to live here, on the
# stated rule "a shape that belongs to the whole application belongs in
# server/audit.py". The Task 6.1 secrets sweep showed that both do: it drove each
# through `audit.redact_detail` and watched them survive, because audit rows and
# `RedactingFilter` are shared surfaces that no Codex module sits on. A
# Codex-local pattern could never have protected them, so both moved upstream and
# there is again exactly one owner per shape.
#
# If a shape ever appears that is genuinely specific to this boundary, add it back
# here — but check first whether an audit row or a log record could carry it, in
# which case it belongs in server/audit.py instead.

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


def scrub_secret_shapes(text: str) -> str:
    """Remove credential-SHAPED substrings, keeping the rest of the text.

    The narrow counterpart to :func:`scrub_text`, for the rare value that must
    stay readable and cannot simply be replaced — currently the CLI version
    string, which is native output from ``codex --version`` yet is rendered on
    ``/info`` and in the Fleet table. Prefer ``scrub_text`` everywhere else:
    pattern matching cannot know that an unrecognised native string is harmless.
    """
    return _shared_redact_text(text)


def contains_secret_shaped_value(text: str) -> bool:
    """Report whether text carries anything the redactors would have to remove."""
    return scrub_secret_shapes(text) != text


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
