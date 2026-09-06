"""MCP registry primitives shared by every in-process `ccswitch` MCP surface.

Two surfaces mount a server under the wire name ``ccswitch`` — the assistant drawer
(``server/assistant/tools.py``) and the coding session (``server/sessions/tools/``).
They never coexist in one session, so they share the name and the ``mcp__ccswitch__``
prefix; what they must NOT share is a duplicated copy of these primitives. The
permission layer keys its read/mutation/destructive rule on ``annotation_for``, so
every surface registers its spec table here and the lookup consults all of them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mcp.types import ToolAnnotations

MCP_PREFIX = "mcp__ccswitch__"
SERVER_NAME = "ccswitch"


class ToolClass(str, Enum):
    READ = "read"
    MUTATION = "mutation"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool_class: ToolClass
    title: str          # human label for permission cards ("Archive session")


class ToolArgError(ValueError):
    """An argument a handler rejects LOCALLY, before any side effect — so the model gets
    a corrective message and nothing downstream ever sees the call."""


def tool_err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def annotations_for(spec: ToolSpec) -> ToolAnnotations:
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.tool_class is ToolClass.READ,
        destructiveHint=spec.tool_class is ToolClass.DESTRUCTIVE,
        idempotentHint=spec.tool_class is ToolClass.READ,
        openWorldHint=False,
    )


_SURFACES: dict[str, Mapping[str, ToolSpec]] = {}


def register_surface(name: str, specs: Mapping[str, ToolSpec]) -> None:
    """Register one surface's spec table. Tool names are unique ACROSS surfaces: the
    permission layer sees only ``mcp__ccswitch__<tool>``, so two surfaces defining the
    same name would be gated by whichever registered first."""
    for other, table in _SURFACES.items():
        if other == name:
            continue
        clash = set(table) & set(specs)
        if clash:
            raise ValueError(f"tool name(s) {sorted(clash)} already registered by surface {other!r}")
    _SURFACES[name] = specs


def surfaces() -> dict[str, Mapping[str, ToolSpec]]:
    return dict(_SURFACES)


def annotation_for(mcp_name: str) -> ToolSpec | None:
    if not mcp_name.startswith(MCP_PREFIX):
        return None
    short = mcp_name[len(MCP_PREFIX):]
    for table in _SURFACES.values():
        spec = table.get(short)
        if spec is not None:
            return spec
    return None
