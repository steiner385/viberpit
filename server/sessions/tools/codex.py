"""Codex twin of the coding-session tool surface: the same handlers declared as
`dynamicTools` on `thread/start` (session tools spec §3.4).

A *sibling* of :mod:`server.sessions.tools.server`, selected behind the engine, for
the reason spelled out in :mod:`server.assistant.options_codex`: Codex has no
in-process MCP server, so the app declares the tools on the thread and executes
them from the ``item/tool/call`` callback. The tool table itself — descriptions,
schemas, handlers, permission annotations — is :func:`build_session_tools`,
unchanged and unduplicated, so adding a session tool reaches both engines at once.

**Declared once, kept for the thread's life.** ``dynamicTools`` exists only on
``ThreadStartParams``, and needs nothing more: codex-cli 0.153.2 saves a thread's
declared tools with its session metadata and restores them on ``thread/resume`` —
verified on a live thread that called a declared tool after two resumes
(docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md, 2026-10-05
addendum). The flip side: a thread keeps the set it STARTED with, so a thread
started before a tool was added resumes without it — which is why the marker
parsers stay permanent.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.assistant.options_codex import ToolHandler, input_schema_for, run_declared_tool
from server.codex import schema
from server.mcp_spec import MCP_PREFIX

from .context import SessionToolContext
from .tools import build_session_tools


@dataclass(frozen=True)
class CodexSessionTools:
    """One coding session's declared tool surface plus the handlers behind it.

    Held as data, like ``CodexAssistantOverlay``, so the driver applies it at exactly
    one place (``thread/start``) and a test can assert the whole shape.

    **``CodexAgentDriver._on_dynamic_tool`` is written against this trio —
    ``declares``, ``mcp_name``, ``run_tool`` — and takes EITHER this or a
    ``CodexAssistantOverlay`` without branching on which.** That is what keeps one
    permission policy across both surfaces, and it means the three must keep answering
    identically on both types: ``mcp_name`` from the shared ``MCP_PREFIX``, ``run_tool``
    from the shared :func:`run_declared_tool`. Changing one of them here without the
    other is how the drawer and a coding session would end up gated differently.
    """

    dynamic_tools: tuple[schema.FunctionDynamicToolSpec, ...]
    handlers: Mapping[str, ToolHandler]

    @classmethod
    def build(cls, ctx: SessionToolContext) -> CodexSessionTools:
        tools = build_session_tools(ctx)
        specs = tuple(
            schema.FunctionDynamicToolSpec(
                name=t.name, description=t.description, input_schema=input_schema_for(t))
            for t in tools
        )
        return cls(dynamic_tools=specs, handlers={t.name: t.handler for t in tools})

    def declares(self, tool: str) -> bool:
        """Whether *tool* is one this surface declared."""
        return tool in self.handlers

    def mcp_name(self, tool: str) -> str:
        """The canonical ``mcp__<brand>__*`` name for *tool*.

        The permission layer keys on this name (``PermissionRegistry.request``'s
        own-MCP branch reads the tool's read/mutation/destructive
        annotation from ``server.mcp_spec``), so a Codex tool call is gated by the
        SAME rule and renders the SAME card as its Claude twin.
        """
        return MCP_PREFIX + tool

    async def run_tool(self, tool: str, arguments: Any) -> schema.DynamicToolCallResponse:
        """Execute one declared tool. Never raises.

        The body is :func:`server.assistant.options_codex.run_declared_tool`, shared
        with the assistant drawer rather than restated: every decision in it (unknown
        name, argument normalization, exception containment, never fabricating a
        success) is a safety decision that must be identical on both surfaces.
        """
        return await run_declared_tool(self.handlers, tool, arguments,
                                       log_label="codex session tools")
