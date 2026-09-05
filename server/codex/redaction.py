"""Centralized redaction for the Codex app-server boundary.

This module is the **only** place `server/codex/` decides what may reach a log,
an exception message, a `repr`, or a support bundle. It deliberately does not
implement its own credential regexes for shapes the application already knows:
`scrub_text()` delegates to `server.audit.redact_text` (the canonical redactor
installed on the root logger by `server.obs.jsonlog.configure_logging`) and then
applies only the two extra shapes that the Codex boundary introduces and the
shared patterns do not already cover end to end:

* bare `sk-`-prefixed OpenAI keys (the shared list covers `sk-ant-` only), and
* short JWTs whose segments fall under the shared 3-part cookie pattern's
  minimum segment lengths.

Everything else — `sk-ant-*`, `ccenc1:*`, padded base64 blobs, long signed
triples — is already handled upstream, so there is exactly one owner per shape.

`Redacted` is the carrier for values that must never render: its `__repr__`,
`__str__`, and `__format__` all return `[REDACTED]`, so a dataclass holding one
is safe under the default dataclass `repr` and under `logging`'s `%s`.

Caller note. `scrub_text`, `scrub_exception`, and `Redacted` are used throughout
`protocol.py` and `schema.py`. `scrub_value`, `SECRET_FIELD_NAMES`, and
`CONTENT_FIELD_NAMES` deliberately have no caller yet: they exist for the two
places that will log whole wire payloads — the process supervisor's crash/stderr
diagnostics (Task 2.2) and the event translator's unknown-item notice (Task 4.1).
They are covered by tests and must be deleted rather than left orphaned if those
tasks end up not needing them.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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

# Wire field names whose values are credentials or native identifiers.
SECRET_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "accessToken",
        "access_token",
        "refreshToken",
        "refresh_token",
        "idToken",
        "id_token",
        "apiKey",
        "api_key",
        "secretAccessKey",
        "sessionToken",
        "chatgptAccountId",
        "chatgpt_account_id",
        "accountId",
        "account_id",
        "previousAccountId",
        "previous_account_id",
        "loginId",
        "login_id",
        "threadId",
        "thread_id",
        "turnId",
        "turn_id",
        "itemId",
        "item_id",
        "email",
        "authUrl",
        "userCode",
        "verificationUrl",
    }
)

# Wire field names whose values are user or model content.
CONTENT_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "input",
        "text",
        "delta",
        "message",
        "output",
        "stdout",
        "stderr",
        "content",
        "command",
        "cmd",
        "argv",
        "patch",
        "diff",
        "changes",
        "path",
        "cwd",
        "developerInstructions",
        "instructions",
        "additionalDetails",
        "summary",
        "reasoning",
    }
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
    """Redact credential-shaped material from arbitrary diagnostic text."""
    scrubbed = _shared_redact_text(text)
    for pattern in _CODEX_PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def scrub_value(value: Any) -> Any:
    """Return a log-safe copy of a wire value.

    Secret- and content-named keys collapse to ``[REDACTED]`` wholesale; every
    other string is still passed through :func:`scrub_text` so a token that
    arrives under an unexpected key is caught too.
    """
    if isinstance(value, Redacted):
        return REDACTED
    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in SECRET_FIELD_NAMES or key in CONTENT_FIELD_NAMES:
                scrubbed[key] = REDACTED
            else:
                scrubbed[key] = scrub_value(item)
        return scrubbed
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (bytes, bytearray)):
        return REDACTED
    if isinstance(value, Sequence):
        return [scrub_value(item) for item in value]
    return value


def contains_secret_shaped_value(text: str) -> bool:
    """Report whether text carries anything the redactors would have to remove."""
    return scrub_text(text) != text


def scrub_exception(exc: BaseException) -> str:
    """Render an exception as one safe single-line ``Type: message`` string."""
    message = scrub_text(str(exc)).replace("\r", " ").replace("\n", " ").strip()
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"
