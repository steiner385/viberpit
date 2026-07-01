"""Automation contract value types + pure config validation (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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


def _type_ok(field_type: str, value: Any) -> bool:
    if field_type == "bool":
        return isinstance(value, bool)
    if field_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type in ("string", "enum"):
        return isinstance(value, str)
    if field_type == "repo-multiselect":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return False


def validate_config(schema: ConfigSchema, config: dict) -> dict:
    """Validate `config` against `schema`. Returns a coerced dict with every
    schema key present (defaults filled). Raises ValueError on an unknown key,
    a wrong type, or a value outside an enum's choices."""
    known = {f.key: f for f in schema}
    for key in config:
        if key not in known:
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


@runtime_checkable
class Automation(Protocol):
    id: str
    name: str
    description: str
    modes: tuple[str, ...]
    config_schema: ConfigSchema

    def status(self, ctx: "Any") -> AutomationStatus: ...
    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: "Any") -> None: ...
