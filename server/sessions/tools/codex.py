"""Codex twin of the coding-session tool surface: the same handlers declared as
`dynamicTools` on `thread/start` (session tools spec §3.4).

A *sibling* of :mod:`server.sessions.tools.server`, selected behind the engine, for
the reason spelled out in :mod:`server.assistant.options_codex`: Codex has no
in-process MCP server, so ccswitch declares the tools on the thread and executes
them from the ``item/tool/call`` callback. The tool table itself — descriptions,
schemas, handlers, permission annotations — is :func:`build_session_tools`,
unchanged and unduplicated, so adding a session tool reaches both engines at once.

**The pinned ``thread/resume`` has no ``dynamicTools`` field, so a RESUMED Codex
coding session has no session tools until the vendor contract gains them.** That is
verified against codex-cli 0.153.2, not assumed
(docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md: ``dynamicTools``
appears on ``ThreadStartParams`` and nowhere else in the v2 surface, and an unknown
field on ``thread/resume`` is accepted and ignored rather than refused — so there is
nothing to send and no error to detect if we sent it). The assistant answers the same
gap by never resuming; a coding session cannot, because its thread *is* the work. So
it resumes without the tools and says so once in the journal
(``codex.driver.session_tools_unavailable_on_resume``).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.assistant.options_codex import ToolHandler, _as_result, input_schema_for, tool_error
from server.codex import schema
from server.mcp_spec import MCP_PREFIX

from .context import SessionToolContext
from .tools import build_session_tools

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexSessionTools:
    """One coding session's declared tool surface plus the handlers behind it.

    Held as data, like ``CodexAssistantOverlay``, so the driver applies it at exactly
    one place (``thread/start``) and a test can assert the whole shape. It answers the
    same three questions the driver's ``_on_dynamic_tool`` asks of either surface —
    ``declares``, ``mcp_name``, ``run_tool`` — which is why that handler needs no
    branch for which one it is serving.
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
        """The canonical ``mcp__ccswitch__*`` name for *tool*.

        The permission layer keys on this name (``PermissionRegistry.request``'s
        ``mcp__ccswitch__`` branch reads the tool's read/mutation/destructive
        annotation from ``server.mcp_spec``), so a Codex tool call is gated by the
        SAME rule and renders the SAME card as its Claude twin.
        """
        return MCP_PREFIX + tool

    async def run_tool(self, tool: str, arguments: Any) -> schema.DynamicToolCallResponse:
        """Execute one declared tool. Never raises.

        Deliberately the same decisions as ``CodexAssistantOverlay.run_tool``: an
        unknown name and a non-object ``arguments`` are tool *errors*, not exceptions,
        because a failed turn is worse for the model than being told the call did not
        happen — and a fabricated success is worse than both.
        """
        handler = self.handlers.get(tool)
        if handler is None:
            log.warning("codex session tools: unknown tool %r", tool)
            return tool_error(f"ccswitch tool error: no tool named {tool!r}")
        if arguments is None:
            args: dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            args = dict(arguments)
        else:
            return tool_error("ccswitch tool error: arguments must be an object")
        try:
            return _as_result(await handler(args))
        except Exception as exc:  # noqa: BLE001 - a tool must never fail the turn
            log.warning("codex session tool %s failed", tool, exc_info=True)
            return tool_error(f"ccswitch tool error: {exc.__class__.__name__}")
