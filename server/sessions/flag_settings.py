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
#: An output style (D-16a) the session's CLI did not list. The CLI accepts ANY name
#: silently, so ViberPit refuses it before anything is sent.
OUTPUT_STYLE_UNKNOWN = "OUTPUT_STYLE_UNKNOWN"
#: Output styles were asked for on an engine without them (Codex), a kind that never
#: takes one (the Assistant drawer), or a session whose CLI has not listed them yet.
OUTPUT_STYLE_UNAVAILABLE = "OUTPUT_STYLE_UNAVAILABLE"
SETTINGS_CODES = frozenset({SETTINGS_UNAVAILABLE, SETTINGS_REFUSED, SETTINGS_INVALID,
                            SETTINGS_FAILED, FAST_UNAVAILABLE, OUTPUT_STYLE_UNKNOWN,
                            OUTPUT_STYLE_UNAVAILABLE})

#: Seconds to wait for one ``apply_flag_settings`` reply. It is a local settings write,
#: answered in milliseconds; the SDK's 60 s default would hold a route for a minute.
SETTINGS_CONTROL_TIMEOUT_S = 15.0

#: The flag-layer keys ViberPit writes. Nothing else is ever sent.
FAST_MODE_KEY = "fastMode"
EFFORT_KEY = "effortLevel"
OUTPUT_STYLE_KEY = "outputStyle"

#: The CLI's own style name for "no style" (D-16 spike §3.1). A spawn never passes it.
DEFAULT_OUTPUT_STYLE = "default"
#: The CLI's built-in styles, in its ``initialize.available_output_styles`` order (D-16
#: spike §3.1, probe 1). The Settings default is chosen from these plus the shared user
#: dir, since the edge has no live session to ask; a live session's own list is the
#: authority for that session.
BUILTIN_OUTPUT_STYLES: tuple[str, ...] = (
    DEFAULT_OUTPUT_STYLE, "Proactive", "Concise", "Explanatory", "Learning")


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


FAST_MODE_STATES = ("on", "off", "cooldown")


def live_fast_mode(source: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """The CLI's LIVE fast-mode state — ``(state, disabled_reason)`` — from any frame
    that carries it (the ``initialize`` reply, ``system/init``). ``state`` is one of
    ``FAST_MODE_STATES`` or None (absent/unrecognised: leave the last reading alone);
    the reason (``out_of_credits``, ``org_level_disabled`` …) is a short string or None.
    The stored choice is ``SessionRecord.fast_mode``; this is what the CLI is doing."""
    if not isinstance(source, Mapping):
        return None, None
    state = source.get("fast_mode_state")
    if state not in FAST_MODE_STATES:
        return None, None
    reason = source.get("fast_mode_disabled_reason")
    return state, (reason if isinstance(reason, str) and reason else None)


def output_styles(server_info: Mapping[str, Any] | None) -> list[str] | None:
    """The style names the CLI's ``initialize`` lists (``available_output_styles``: the
    built-ins, user and project styles, ``<plugin>:<name>`` plugin styles), or None when
    the reply does not carry a list. The per-session cache ``SessionRecord.output_styles``
    holds this, and every name ViberPit sends is checked against it."""
    styles = server_info.get("available_output_styles") if isinstance(
        server_info, Mapping) else None
    if not isinstance(styles, list):
        return None
    return [s for s in styles if isinstance(s, str) and s]


def live_output_style(server_info: Mapping[str, Any] | None) -> str | None:
    """The style the CLI started with (``initialize.output_style``), or None."""
    style = server_info.get("output_style") if isinstance(server_info, Mapping) else None
    return style if isinstance(style, str) and style else None


def takes_output_style(record: Any) -> bool:
    """Can *record*'s session carry an output style at all: a Claude coding session with
    the ``VIBERPIT_FLAG_SETTINGS_ENABLED`` kill switch on. Codex has no styles, and the
    Assistant drawer's prompt is ViberPit's own. Shared by the orchestrator gate (which
    also checks the engine capability) and the session payload."""
    from server.sessions.engine import AgentEngine, flag_settings_enabled
    from server.sessions.types import is_assistant
    return (getattr(record, "engine", None) is AgentEngine.CLAUDE_CODE
            and not is_assistant(record) and flag_settings_enabled())


def available_output_styles(record: Any) -> list[str]:
    """The styles *record*'s session can switch to: its CLI's cached list when it takes
    styles at all (``takes_output_style``), else []. [] also when no list is known."""
    if not takes_output_style(record):
        return []
    return list(getattr(record, "output_styles", None) or [])


def effective_output_style(record: Any) -> str | None:
    """The style *record*'s session runs, as the payload's ``outputStyle`` serves it:
    the CLI's live reading, else the stored choice — ``default`` when the CLI's list is
    known and lacks it — else None (not known yet)."""
    live = getattr(record, "output_style_live", None)
    if live:
        return live
    stored = getattr(record, "output_style", None)
    if not stored:
        return None
    known = getattr(record, "output_styles", None)
    return stored if known is None or stored in known else DEFAULT_OUTPUT_STYLE


def supports_fast_mode(fast_models: Iterable[str] | None, model: str | None) -> bool:
    """Does *model* have fast mode, per a cached ``fast_mode_models`` list? An unknown
    list (None) is never a yes: availability is only what ``initialize`` reported."""
    return fast_models is not None and _model_key(model) in set(fast_models)


def model_options(server_info: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The ``initialize.models[]`` rows trimmed to what ViberPit reads: ``value``,
    ``resolvedModel``, ``supportsFastMode`` and ``supportedEffortLevels``. The per-session
    cache ``SessionRecord.model_options`` (daemon-local) holds this list."""
    keys = ("value", "resolvedModel", "supportsFastMode", "supportedEffortLevels")
    return [{k: row[k] for k in keys if k in row} for row in _model_rows(server_info)]


def fast_mode_available(fast_models: Iterable[str] | None, model: str | None) -> bool:
    """THE fast-mode availability computation, shared by the session payload and the
    orchestrator gate: the ``VIBERPIT_FLAG_SETTINGS_ENABLED`` kill switch is on AND the
    cached list marks *model* fast-capable. (Only a flag-settings engine fills the list;
    the orchestrator also checks the engine capability.)"""
    from server.sessions.engine import flag_settings_enabled
    return flag_settings_enabled() and supports_fast_mode(fast_models, model)


def effort_levels(server_info: Mapping[str, Any] | None,
                  model: str | None) -> list[str] | None:
    """The effort levels the live model accepts, or None when the list does not say."""
    row = model_entry(server_info, model)
    levels = row.get("supportedEffortLevels") if row is not None else None
    return [str(v) for v in levels] if isinstance(levels, list) else None
