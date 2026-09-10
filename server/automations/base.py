"""Automation contract value types + pure config validation (no I/O)."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

FIELD_TYPES = {"bool", "int", "string", "enum", "repo-multiselect"}


@dataclass
class ConfigField:
    key: str
    label: str
    type: str
    default: Any
    help: str = ""
    choices: list[str] | None = None  # required for type == "enum"

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type,
                "default": self.default, "help": self.help, "choices": self.choices}


ConfigSchema = list[ConfigField]


@dataclass
class AutomationStatus:
    text: str
    level: str = "idle"  # idle | active | warn | error
    counts: dict | None = None

    def to_dict(self) -> dict:
        return {"text": self.text, "level": self.level, "counts": self.counts}


@dataclass
class ActionSpec:
    """One trigger button on an actionable item."""
    kind: str
    label: str
    style: str = "default"  # "default" | "risky" (risky → confirm + distinct color)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "style": self.style}


@dataclass
class ActionableItem:
    """One row in an automation's actionable list."""
    id: str
    title: str
    subtitle: str | None = None
    url: str | None = None
    actions: list[ActionSpec] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "subtitle": self.subtitle,
                "url": self.url, "actions": [a.to_dict() for a in self.actions]}


@dataclass
class TriggerResult:
    """The outcome of a manual trigger — surfaced to the user as toast + inline."""
    status: str          # machine-readable (e.g. "spawned", "ceiling", "failed")
    level: str           # "ok" | "warn" | "error"
    message: str         # human-readable
    # Async triggers (issue #668) answer "queued" immediately and carry a job id
    # the UI polls for live phase progress. Omitted from the payload when unset so
    # synchronous results are byte-for-byte unchanged.
    job_id: str | None = None
    phase: str | None = None

    def to_dict(self) -> dict:
        d = {"status": self.status, "level": self.level, "message": self.message}
        if self.job_id is not None:
            d["job_id"] = self.job_id
        if self.phase is not None:
            d["phase"] = self.phase
        return d


def _type_ok(field_type: str, value: Any) -> bool:
    if field_type == "bool":
        return isinstance(value, bool)
    if field_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type in ("string", "enum"):
        return isinstance(value, str)
    if field_type == "repo-multiselect":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if field_type == "repo-number-map":
        # {repo: number} — per-repo weights / caps. bool is an int subclass, so
        # reject it explicitly or a stray True would masquerade as the number 1.
        return isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
            for k, v in value.items())
    return False


def validate_config(schema: ConfigSchema, config: dict,
                    *, retired: Iterable[str] = ()) -> dict:
    """Validate `config` against `schema`. Returns a coerced dict with every
    schema key present (defaults filled). Raises ValueError on an unknown key,
    a wrong type, or a value outside an enum's choices.

    *retired* names keys a schema deliberately DROPPED (``retired_config_keys``).
    Rejecting them would be indistinguishable from a corrupt row: every reader
    falls back to pure schema defaults on a ValueError, so removing one field
    would silently reset an automation's whole config — for the backlog runner
    that means ``enabled_repos: []``, a card reading "0 eligible" over a full
    backlog with nothing in the logs. So a retired key is dropped and logged.
    A key that is merely unknown (a typo, a foreign row) still raises: retiring
    is an explicit, per-automation list, never a general amnesty.
    """
    known = {f.key: f for f in schema}
    retired_keys = frozenset(retired)
    for key in config:
        if key in known:
            continue
        if key in retired_keys:
            log.info("config.retired_key_dropped key=%s", key)
            continue
        raise ValueError(f"unknown config key: {key!r}")
    out: dict = {}
    for f in schema:
        if f.key in config:
            value = config[f.key]
            if not _type_ok(f.type, value):
                raise ValueError(f"{f.key}: expected {f.type}, got {type(value).__name__}")
            if f.type == "enum" and value not in (f.choices or []):
                raise ValueError(f"{f.key}: {value!r} not in {f.choices}")
            out[f.key] = value
        else:
            out[f.key] = f.default
    return out


#: What kind of work an origin's sessions are, for the sessions list's
#: foreground/background split. ``development`` produces new work the operator
#: is steering toward (a backlog issue, a spec, a plan); ``maintenance`` is
#: housekeeping that should stay out of the way until it needs a human.
WORK_CLASSES: tuple[str, ...] = ("development", "maintenance")
DEFAULT_WORK_CLASS = "development"


def work_class_of(automation: Any) -> str:
    """The work class *automation* declares — see ``Automation.work_class``.

    One accessor rather than a ``getattr`` at each reader, so the automations
    payload, the session serializer and the browser cannot drift on which
    origins are maintenance. The default is the OPPOSITE of ``engine_policy``'s
    fail-closed: an origin that forgot to declare is ``development``, because
    the safe mistake here is a maintenance session that stays visible, never a
    development session that is hidden. A value outside ``WORK_CLASSES`` is a
    typo and raises rather than silently landing in the foreground.
    """
    value = getattr(automation, "work_class", None)
    if value is None:
        return DEFAULT_WORK_CLASS
    if value not in WORK_CLASSES:
        raise ValueError(
            f"{getattr(automation, 'id', automation)!r}: work_class {value!r} "
            f"not in {WORK_CLASSES}")
    return value


def retired_config_keys_of(automation: Any) -> tuple[str, ...]:
    """The keys *automation* declares retired — see ``Automation.retired_config_keys``.

    An accessor rather than a ``getattr`` per call site, so the API's write path
    and the loops' read path cannot disagree about which stored keys are dead
    (one dropping a key the other 422s on would be worse than either behaviour
    alone). Most automations never retired a field and declare nothing.
    """
    return tuple(getattr(automation, "retired_config_keys", ()) or ())


def operator_triggered(automation: Any) -> bool:
    """Whether *automation* has no control loop — see ``Automation.modes``.

    One accessor rather than an ``if not a.modes`` spelled out at each reader, so
    the API's refusal, the list payload and the UI cannot drift on what "no
    modes" means.
    """
    return not tuple(getattr(automation, "modes", ()) or ())


@runtime_checkable
class Automation(Protocol):
    id: str
    name: str
    description: str
    #: The active modes this origin's control loop may run in. **Empty means the
    #: origin has no control loop** — it spawns only when an operator (or another
    #: automation) calls its seam, so there is nothing to enable and nothing to
    #: schedule. ``operator_triggered`` below is the accessor; the API refuses an
    #: enabled/mode PATCH for such an origin and the card renders no toggle,
    #: rather than showing a switch whose position means nothing.
    modes: tuple[str, ...]
    #: ``"development"`` or ``"maintenance"`` (``WORK_CLASSES``): whether the
    #: sessions this origin spawns are new work the operator steers, or routine
    #: housekeeping the sessions list keeps in the background until one needs a
    #: human. Read only through ``work_class_of`` (defaults to development).
    work_class: str
    config_schema: ConfigSchema
    #: OPTIONAL sibling of ``config_schema``, declared by an automation as
    #: ``retired_config_keys: tuple[str, ...]``: keys its schema USED to have and
    #: deliberately dropped. Stored rows outlive a schema change and
    #: ``validate_config`` rejects unknown keys, so without the list, removing a
    #: field silently resets every existing row to defaults. Not a member of this
    #: Protocol — a declared member is mandatory for every implementer, and
    #: almost none has ever retired a field; ``retired_config_keys_of`` above is
    #: the accessor and answers ``()`` for those.

    #: The audited agent-engine ceiling for this origin (Phase 7, Task 7.1).
    #: Typed loosely here because ``engine_policy`` imports this module for
    #: ``ConfigField``; ``server.automations.engine_policy.policy_for`` is the
    #: accessor, and it answers ``claude_only`` for an automation that declares
    #: nothing — an oversight must fail closed, never open.
    engine_policy: Any

    def status(self, ctx: "Any") -> AutomationStatus: ...
    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: "Any") -> None: ...


@runtime_checkable
class ActionProvider(Protocol):
    """An automation's manual-action surface: list items, trigger one action."""
    valid_kinds: frozenset[str]

    async def actionable(self) -> list[ActionableItem]: ...
    async def trigger(self, item_id: str, kind: str) -> TriggerResult: ...
