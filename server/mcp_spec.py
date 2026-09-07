"""MCP registry primitives shared by every one of the app's in-process MCP surfaces.

Two surfaces mount a server under ``brand.MCP_SERVER_NAME`` — the assistant drawer
(``server/assistant/tools.py``) and the coding session (``server/sessions/tools/``).
They never coexist in one session, so they share the name and the tool prefix; what
they must NOT share is a duplicated copy of these primitives. The permission layer
keys its read/mutation/destructive rule on ``annotation_for``, so every surface
registers its spec table here and the lookup consults all of them.

The wire name itself is NOT spelled here: ``src/ccswitch/brand.py`` owns it, and the
names below are aliases so a reader of this module does not have to know which of the
two dual-accept prefixes is the emitted one.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mcp.types import ToolAnnotations

from ccswitch.brand import MCP_SERVER_NAME as SERVER_NAME
from ccswitch.brand import MCP_TOOL_PREFIX as MCP_PREFIX
from ccswitch.brand import MCP_TOOL_PREFIXES, is_mcp_tool, mcp_tool_stem

__all__ = ["MCP_PREFIX", "MCP_TOOL_PREFIXES", "SERVER_NAME", "ToolArgError", "ToolClass",
           "ToolSpec", "annotation_for", "annotations_for", "is_mcp_tool",
           "mcp_tool_stem", "register_surface", "surfaces", "tool_err"]


class ToolClass(str, Enum):
    READ = "read"
    MUTATION = "mutation"
    DESTRUCTIVE = "destructive"
    #: Reports the session's OWN state back to the harness (completion, no-PR,
    #: a model request). No permission card: these replace HTML-comment markers,
    #: which had no gate at all, and a card at the end of a turn would block an
    #: unattended session behind a prompt nobody is there to answer. Not READ —
    #: a signal moves a status badge, and annotating that read-only is false.
    SIGNAL = "signal"


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
        idempotentHint=spec.tool_class in (ToolClass.READ, ToolClass.SIGNAL),
        openWorldHint=False,
    )


_SURFACES: dict[str, Mapping[str, ToolSpec]] = {}


def register_surface(name: str, specs: Mapping[str, ToolSpec]) -> None:
    """Register one surface's spec table. Tool names are unique ACROSS surfaces: the
    permission layer sees only the prefixed ``mcp__<brand>__<tool>``, so two surfaces
    defining the same name would be gated by whichever registered first."""
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
    """The spec for *mcp_name* under EITHER brand prefix, across every surface.

    Both halves matter and for different reasons. Accepting either prefix is the
    permanent compat side of the rebrand (``brand.mcp_tool_stem``): a resumed thread
    or a replayed transcript still carries pre-rebrand names, and a None here would
    downgrade a DESTRUCTIVE tool's card to the grantable mutation branch. Consulting
    every surface is what lets one permission rule cover the drawer and a coding
    session without either growing its own copy of it.
    """
    short = mcp_tool_stem(mcp_name)
    if short is None:
        return None
    for table in _SURFACES.values():
        spec = table.get(short)
        if spec is not None:
            return spec
    return None
