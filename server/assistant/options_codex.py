"""The Codex overlay for an assistant-kind session (#1321, spec §4.1 / §4.9 B7·G8).

A *sibling* of :mod:`server.assistant.options`, not a branch inside it. The Claude
overlay is five concepts — ``tools=[]``, ``disallowed_tools``, ``mcp_servers``,
``strict_mcp_config``, ``system_prompt`` — and only the last two have a Codex
equivalent. Codex preserves the same security property by a different mechanism,
so it gets its own reviewed surface selected behind the engine
(:func:`server.assistant.options.assistant_overlay_for`):

===========================================  ==========================================
Claude                                       Codex (verified, codex-cli 0.153.2)
===========================================  ==========================================
``mcp_servers={"ccswitch": server}``         ``thread/start`` ``dynamicTools`` —
(in-process SDK MCP server)                  client-declared specs, called back as the
                                             server→client request ``item/tool/call``.
                                             No child process, no config file.
``can_use_tool`` gate                        **structural**: ccswitch executes every
                                             ccswitch tool, so a tool cannot run
                                             without ccswitch answering the callback.
``tools=[]`` + ``disallowed_tools``          **no equivalent — built-ins cannot be
(built-ins removed)                          removed.** The boundary is
                                             ``approvalPolicy: "untrusted"`` +
                                             ``sandbox: "read-only"`` + a default-deny
                                             approval handler.
``system_prompt`` (plain string, replaces)   ``baseInstructions`` (replaces).
``strict_mcp_config=True``                   n/a — the account's own MCP servers come
                                             from its ``CODEX_HOME`` config.
===========================================  ==========================================

Three findings from the spike (docs/superpowers/spikes/2026-09-06-assistant-codex-findings.md)
are encoded here rather than left to a caller:

* **A restrictive prompt is persuasion, not a boundary.** The spike's control arm —
  identical except that ``baseInstructions`` was dropped — shows the model reaching
  for ``/bin/bash -lc 'ls -la'`` immediately. What stops it is the host answering
  ``decline``; see :func:`assistant_approval_decision`.
* **The neutral ``cwd`` is load-bearing.** ``thread/start`` folds the repository's
  ``AGENTS.md`` into thread context on its own (reported in ``instructionSources``)
  and ``baseInstructions`` does not suppress it — in the control arm the model quoted
  it *after* its shell read was declined. A ``cwd`` outside any repository returns
  ``instructionSources: []``.
* **An assistant thread must never be resumed.** ``thread/resume`` has no
  ``dynamicTools`` field (``v2/ThreadResumeParams.json``), so a resumed thread comes
  back with the built-ins still declared and the ccswitch tools gone. See
  :data:`ASSISTANT_CODEX_RESUME_UNSUPPORTED`.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import SdkMcpTool
from claude_agent_sdk import _python_type_to_json_schema

from ccswitch import brand
from ccswitch import paths as _paths
from server.codex import schema
from server.codex.permissions import ASSISTANT_PROFILE

from .client import AssistantApi
from .prompt import (ASSISTANT_SYSTEM_PROMPT, OPERATOR_TOOLS_DISABLED_LINE,
                     OPERATOR_TOOLS_ENABLED_LINE)
from .tools import MCP_PREFIX, build_tools

log = logging.getLogger(__name__)

#: The sandbox/approval pair for an assistant thread, taken from the one module
#: that owns that mapping (``server/codex/permissions.py``) rather than restated:
#: `thread/start` reads it from here and `turn/start` reads the same profile off
#: the driver, so the launch and every subsequent turn cannot drift apart.
#:
#: Read-only is not a convenience — with no way to remove the built-ins, it is
#: half of what makes a declined approval a real refusal rather than a race — and
#: `untrusted` is what routes EVERY built-in through an approval ccswitch can
#: answer. `on-request` would let the model self-certify a command as safe.
ASSISTANT_CODEX_SANDBOX = ASSISTANT_PROFILE.sandbox_mode.value
ASSISTANT_CODEX_APPROVAL_POLICY = ASSISTANT_PROFILE.approval_policy.value

#: The v2 `item/*` approval decision that means "no". Spelled out here because the
#: vocabulary trap is per-method and silent: `{"decision": "denied"}` — the LEGACY
#: `ReviewDecision`/`ExecCommandApproval` word — is a malformed response the model
#: narrates as "the shell tool's approval request failed … I'll retry with an
#: explicit approval prompt". That reads exactly like a successful deny while being
#: an error, so an overlay that gets it wrong looks secure and is merely broken.
ASSISTANT_CODEX_DECLINE = "decline"

#: Why an assistant thread is started fresh instead of resumed. Surfaced as a
#: constant so the driver's refusal and this module's reasoning are one string.
ASSISTANT_CODEX_RESUME_UNSUPPORTED = (
    "thread/resume cannot re-declare dynamicTools on codex-cli 0.153.2, so a "
    "resumed assistant thread would keep the built-ins and lose every ccswitch "
    "tool; starting a new thread instead"
)

#: Directory name under the ccswitch config root used as an assistant thread's
#: `cwd`. Empty, private, and outside any repository.
NEUTRAL_CWD_DIRNAME = "codex-neutral-cwd"

#: Filenames whose presence anywhere at or above the neutral `cwd` would be folded
#: into an assistant thread's instructions. `.git` is here because Codex's
#: instruction discovery walks to the repository root.
_CONTEXT_MARKERS = (".git", "AGENTS.md", "CLAUDE.md")


class AssistantCodexUnsafeCwd(RuntimeError):
    """The directory chosen as an assistant thread's `cwd` is not neutral.

    Raised rather than warned: a thread started in a directory Codex reads
    instructions from is the confidentiality regression this overlay exists to
    prevent, and "start it anyway and log" is how that becomes permanent.
    """


def _marker_above(path: str) -> str | None:
    """The first context marker at or above *path*, or ``None``."""
    current = os.path.abspath(path)
    while True:
        for marker in _CONTEXT_MARKERS:
            candidate = os.path.join(current, marker)
            if os.path.exists(candidate):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def neutral_cwd() -> str:
    """An empty, private directory outside any repository, created on demand.

    Lives under the ccswitch config root (``ccswitch.paths.cfg_root()``) rather
    than a temp dir so the same path is reused across restarts and shows up in an
    operator's own state directory instead of ``/tmp``.
    """
    target = os.path.join(str(_paths.cfg_root()), "assistant", NEUTRAL_CWD_DIRNAME)
    os.makedirs(target, mode=0o700, exist_ok=True)
    marker = _marker_above(target)
    if marker is not None:
        raise AssistantCodexUnsafeCwd(
            f"{target} would inherit agent instructions from {marker}; an assistant "
            "thread must start outside any repository"
        )
    return target


def assistant_approval_decision(method: str) -> schema.ApprovalResponse:
    """The default-deny answer to one built-in approval request.

    There is no branch and no policy lookup: an assistant thread declares no
    built-in it is allowed to use, so every ``item/commandExecution/requestApproval``
    and ``item/fileChange/requestApproval`` it raises is the model reaching outside
    its surface. That is a prompt-injection surface, not a user decision, so — like
    ``ClaudeAgentDriver._can_use_tool``'s hard deny for a non-``mcp__viberpit__``
    tool — it never reaches the permission registry and never renders a card.
    """
    if method == schema.METHOD_COMMAND_EXECUTION_APPROVAL:
        return schema.CommandExecutionApprovalResponse(decision=ASSISTANT_CODEX_DECLINE)
    if method == schema.METHOD_FILE_CHANGE_APPROVAL:
        return schema.FileChangeApprovalResponse(decision=ASSISTANT_CODEX_DECLINE)
    raise ValueError(f"no assistant deny decision for {method!r}")


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def input_schema_for(tool: SdkMcpTool[Any]) -> dict[str, Any]:
    """The JSON Schema Codex must be handed for *tool*.

    Most ccswitch tools build an explicit schema (``tools._schema``) because they
    have optional arguments, but the all-required ones use the SDK's *shorthand*
    — ``{"id": str}`` — which is a mapping of python types, not JSON Schema. The
    Claude path never sees the difference: ``create_sdk_mcp_server`` expands the
    shorthand at server-build time. Codex has no such expander, so declaring the
    shorthand verbatim would put a python ``type`` object on the wire.

    The leaf type mapping is the SDK's own
    (``claude_agent_sdk._python_type_to_json_schema``), deliberately reused rather
    than re-tabulated: a third copy of "python type → JSON type" in this
    repository is how the two would drift. Only the object wrapper is restated,
    and ``test_assistant_codex_overlay`` pins the result against the schema the
    Claude MCP server actually publishes, so a divergence fails a test rather
    than silently giving the two engines different tool contracts.
    """
    raw = tool.input_schema
    if not isinstance(raw, Mapping):
        # A TypedDict-shaped schema. `build_tools` uses neither form today, and a
        # tool the model cannot call correctly is worse than one it cannot see.
        raise TypeError(f"assistant tool {tool.name} has no object input schema")
    if isinstance(raw.get("type"), str) and "properties" in raw:
        return dict(raw)
    properties = {name: _python_type_to_json_schema(t) for name, t in raw.items()}
    return {"type": "object", "properties": properties,
            "required": list(properties)}


def tool_error(message: str) -> schema.DynamicToolCallResponse:
    """A failed tool result. `success=False` is how the model is told the call did
    not happen — the one thing it must never be left to infer."""
    return schema.DynamicToolCallResponse(
        success=False,
        content_items=(schema.DynamicToolCallContentItem(text=message),),
    )


def _as_result(payload: Any) -> schema.DynamicToolCallResponse:
    """Render one MCP-shaped tool result as an `item/tool/call` response.

    The handlers built by :func:`server.assistant.tools.build_tools` return the
    MCP content shape (``{"content": [{"type": "text", "text": …}], "is_error": …}``)
    because they are the SAME callables the Claude path mounts — there is one tool
    table, not one per engine. This is the only place that shape is translated.
    """
    if not isinstance(payload, Mapping):
        return tool_error(f"{brand.NAME} tool error: malformed result")
    blocks = payload.get("content")
    texts: list[str] = []
    if isinstance(blocks, str):
        texts.append(blocks)
    elif isinstance(blocks, Sequence):
        for block in blocks:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    if not texts:
        texts.append(json.dumps({"ok": not payload.get("is_error")}, separators=(",", ":")))
    return schema.DynamicToolCallResponse(
        success=not bool(payload.get("is_error")),
        content_items=tuple(schema.DynamicToolCallContentItem(text=t) for t in texts),
    )


async def run_declared_tool(handlers: Mapping[str, ToolHandler], tool: str,
                            arguments: Any, *,
                            log_label: str) -> schema.DynamicToolCallResponse:
    """Execute one host-declared Codex tool. Never raises.

    THE executor behind every `item/tool/call` the app answers, shared by both
    surfaces that declare `dynamicTools` — the assistant drawer
    (:class:`CodexAssistantOverlay`) and a coding session
    (``server.sessions.tools.codex.CodexSessionTools``). One body, because every
    decision in it is a *safety* decision that must be identical on both:

    * an unknown name is a tool error, not an exception — the app-server should only
      ever ask for a declared tool, so this is either a bug or the model confabulating
      one, and both are better answered with "no such tool" than with a failed turn;
    * ``None`` arguments mean "no arguments", a non-object is a tool error;
    * any exception out of a handler becomes a tool error carrying only the exception
      *class*, never its message (which can quote scrubbed internals);
    * nothing here ever fabricates a success — an invented answer is one the model
      will go on to reason from.

    *log_label* names the calling surface in the warning line; it is the only thing
    that differs between the two callers.
    """
    handler = handlers.get(tool)
    if handler is None:
        log.warning("%s: unknown tool %r", log_label, tool)
        return tool_error(f"{brand.NAME} tool error: no tool named {tool!r}")
    if arguments is None:
        args: dict[str, Any] = {}
    elif isinstance(arguments, Mapping):
        args = dict(arguments)
    else:
        return tool_error(f"{brand.NAME} tool error: arguments must be an object")
    try:
        return _as_result(await handler(args))
    except Exception as exc:  # noqa: BLE001 - a tool must never fail the turn
        log.warning("%s tool %s failed", log_label, tool, exc_info=True)
        return tool_error(f"{brand.NAME} tool error: {exc.__class__.__name__}")


@dataclass(frozen=True)
class CodexAssistantOverlay:
    """Everything that makes one Codex thread an assistant rather than a coding agent.

    Held as data rather than applied at construction so the driver can apply it at
    exactly one place (``thread/start``) and tests can assert the whole shape —
    the Codex analogue of the Claude overlay being a plain dict merged over the
    factory's options.
    """

    base_instructions: str
    dynamic_tools: tuple[schema.FunctionDynamicToolSpec, ...]
    handlers: Mapping[str, ToolHandler]
    cwd: str
    model: str | None = None
    sandbox: str = ASSISTANT_CODEX_SANDBOX
    approval_policy: str = ASSISTANT_CODEX_APPROVAL_POLICY

    def thread_start_params(self) -> schema.ThreadStartParams:
        """The `thread/start` params for this assistant thread.

        ``developer_instructions`` is deliberately NOT set: the ccswitch marker
        contracts it carries (completion, PR, self-directed model request) are
        coding-session contracts, and ``base_instructions`` replaces the
        app-server's own agent instructions the way the Claude overlay's plain
        ``system_prompt`` string replaces the ``claude_code`` preset.
        """
        return schema.ThreadStartParams(
            cwd=self.cwd,
            model=self.model,
            sandbox=self.sandbox,
            approval_policy=self.approval_policy,
            base_instructions=self.base_instructions,
            dynamic_tools=self.dynamic_tools,
        )

    def declares(self, tool: str) -> bool:
        """Whether *tool* is one this overlay mounted."""
        return tool in self.handlers

    def mcp_name(self, tool: str) -> str:
        """The canonical ``mcp__viberpit__*`` name for *tool*.

        The permission layer keys on this name
        (``PermissionRegistry.request``'s app-action branch, which reads
        the tool's read/mutation/destructive annotation), so a Codex tool call is
        gated by the SAME rule and renders the SAME card as its Claude twin
        instead of growing a second policy.
        """
        return MCP_PREFIX + tool

    async def run_tool(self, tool: str, arguments: Any) -> schema.DynamicToolCallResponse:
        """Execute one declared tool. Never raises — see :func:`run_declared_tool`,
        which holds the body this shares with the coding-session surface."""
        return await run_declared_tool(self.handlers, tool, arguments,
                                       log_label="assistant codex overlay")


def assistant_codex_options(*, api: AssistantApi, model: str,
                            operator: bool = False) -> CodexAssistantOverlay:
    """Build the Codex overlay for one assistant session.

    *api* is the loopback ``AssistantApi`` the tool handlers close over — the same
    object the Claude path hands to ``build_server``. The tool table itself comes
    from :func:`server.assistant.tools.build_tools`, unchanged and unduplicated, so
    adding a tool reaches both engines at once.

    *operator* must match the flag *api*'s caller resolved
    (``operator_tools_enabled(db)``): it selects both which tools are built and
    which frozen prompt line is appended, so a mismatched pair would describe a
    tool surface the thread does not have.

    Note what is NOT here. Claude's overlay carries ``max_turns``; Codex's
    ``thread/start`` has no turn cap, so the drawer's bound on Codex is the idle
    reap and the operator's own quota. That is a real difference, stated rather
    than papered over with a lookalike field nothing reads.
    """
    tools = build_tools(api, operator=operator)
    specs: list[schema.FunctionDynamicToolSpec] = []
    handlers: dict[str, ToolHandler] = {}
    for spec in tools:
        specs.append(schema.FunctionDynamicToolSpec(
            name=spec.name, description=spec.description,
            input_schema=input_schema_for(spec)))
        handlers[spec.name] = spec.handler
    return CodexAssistantOverlay(
        base_instructions=ASSISTANT_SYSTEM_PROMPT + (
            OPERATOR_TOOLS_ENABLED_LINE if operator else OPERATOR_TOOLS_DISABLED_LINE),
        dynamic_tools=tuple(specs),
        handlers=handlers,
        cwd=neutral_cwd(),
        model=model,
    )
