"""Live CLI flag settings (D-14): the refusal vocabulary and the ``initialize`` model-list
reads shared by the Claude driver, the orchestrator, the daemon RPC and the API route.

The CLI surface is internal (constitution I) and verified against claude 2.1.318 in
``docs/superpowers/specs/2026-11-04-d14-session-settings-verification.md``:

- ``apply_flag_settings {settings: {...}}`` writes the FLAG layer of the running
  session's settings (the same layer ``--settings`` sets at spawn) and answers an empty
  ``success``. ``{"fastMode": bool}`` toggles fast mode; ``{"effortLevel": <level>}``
  changes the effort of the next model request (``null`` clears it to the model
  default). A malformed request is a control ``error``. The CLI does not validate an
  effort level, so the driver checks it against the live model entry.
- Each write must be ANSWERED before the next is sent: back-to-back frames read stale
  values in the spike (§1.3). The driver serializes them.
- ``initialize.models[]`` rows are ``{value, resolvedModel, supportsFastMode?,
  supportedEffortLevels?, …}``. ``supportsFastMode`` is the only authority on which
  models have fast mode; ViberPit never decides that from a model name.

Every refusal raises :class:`SettingsError` with one of ``SETTINGS_CODES``; it carries
``error_data`` (as D-9's ``RewindError`` does) so the code survives the RPC envelope and
the daemon logs it at INFO. ``server/tests/test_settings_cli_contract.py`` re-checks the
wire against the installed binary.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: The session has no live client (or the SDK client lacks the control channel), or its
#: engine has no flag-settings channel at all.
SETTINGS_UNAVAILABLE = "SETTINGS_UNAVAILABLE"
#: The CLI answered the request with a control ``error``: nothing was applied.
SETTINGS_REFUSED = "SETTINGS_REFUSED"
#: The value is not one the session's live model accepts (an unknown effort level).
SETTINGS_INVALID = "SETTINGS_INVALID"
#: No reply came (timeout or transport failure): the write may or may not have landed.
SETTINGS_FAILED = "SETTINGS_FAILED"
#: Fast mode was asked for on an engine or model that does not support it.
FAST_UNAVAILABLE = "FAST_UNAVAILABLE"
SETTINGS_CODES = frozenset({SETTINGS_UNAVAILABLE, SETTINGS_REFUSED, SETTINGS_INVALID,
                            SETTINGS_FAILED, FAST_UNAVAILABLE})

#: Seconds to wait for one ``apply_flag_settings`` reply. It is a local settings write,
#: answered in milliseconds; the SDK's 60 s default would hold a route for a minute.
SETTINGS_CONTROL_TIMEOUT_S = 15.0

#: The flag-layer keys ViberPit writes. Nothing else is ever sent.
FAST_MODE_KEY = "fastMode"
EFFORT_KEY = "effortLevel"


class SettingsError(Exception):
    """An expected flag-settings refusal; ``code`` is one of ``SETTINGS_CODES``."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if code not in SETTINGS_CODES:
            raise ValueError(f"unknown settings code {code!r}")
        super().__init__(message or code)
        self.code = code
        self.error_data: dict[str, Any] = {"code": code}


def _model_key(model: str | None) -> str:
    """The ``initialize.models[]`` identifier *model* is looked up by: no model (or the
    ``default`` alias) is the ``default`` row; a ``[1m]`` context suffix is dropped."""
    key = (model or "").strip()
    if key.endswith("[1m]"):
        key = key[: -len("[1m]")]
    return key or "default"


def _model_rows(server_info: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    rows = (server_info or {}).get("models") if isinstance(server_info, Mapping) else None
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


def model_entry(server_info: Mapping[str, Any] | None,
                model: str | None) -> Mapping[str, Any] | None:
    """The ``initialize.models[]`` row for *model* (matched on ``value`` — the alias —
    or ``resolvedModel`` — the full id), or None when the list does not name it."""
    key = _model_key(model)
    for row in _model_rows(server_info):
        if key in (row.get("value"), row.get("resolvedModel")):
            return row
    return None


def fast_mode_models(server_info: Mapping[str, Any] | None) -> list[str]:
    """Every identifier (alias and full id) of a model the CLI says has fast mode —
    the per-session cache ``SessionRecord.fast_mode_models`` holds. Sorted, so equal
    lists compare equal."""
    ids: set[str] = set()
    for row in _model_rows(server_info):
        if row.get("supportsFastMode") is True:
            ids.update(v for v in (row.get("value"), row.get("resolvedModel"))
                       if isinstance(v, str) and v)
    return sorted(ids)


def supports_fast_mode(fast_models: Iterable[str] | None, model: str | None) -> bool:
    """Does *model* have fast mode, per a cached ``fast_mode_models`` list? An unknown
    list (None) is never a yes: availability is only what ``initialize`` reported."""
    return fast_models is not None and _model_key(model) in set(fast_models)


def effort_levels(server_info: Mapping[str, Any] | None,
                  model: str | None) -> list[str] | None:
    """The effort levels the live model accepts, or None when the list does not say."""
    row = model_entry(server_info, model)
    levels = row.get("supportedEffortLevels") if row is not None else None
    return [str(v) for v in levels] if isinstance(levels, list) else None
