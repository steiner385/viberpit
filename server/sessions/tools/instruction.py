"""THE one composition of "engine instructions + session-tools paragraph" — the single
point where a session's system prompt learns how to signal completion, no-PR, and a model
request, either through markers or through the signalling tools, never both.

Tool detail lives in the tool descriptions, which the model reads at call time; the
tools paragraph here is an index naming what is mounted, not a spec. Shared task
follow-through policy is appended independently of the signal transport.

Both the product name and the MCP server name come from ``viberpit.brand``: this
paragraph tells the model which server to look under, so a hand-typed name would
point it at a server that is not mounted."""
from __future__ import annotations

from viberpit import brand

from ..completion import COMPLETION_MARKER, marker_instructions
from .registry import mounted_names

# Shared task policy, independent of the engine and completion signal transport.
# Keep tool-specific mechanics in the tool descriptions / marker contracts below.
FOLLOW_THROUGH_INSTRUCTION = """Problem-solving follow-through
Treat a report of malfunction, a question about why something is broken, or a request
to investigate an issue as a request to help resolve that problem. Establish the
cause, then carry a clear, in-scope remedy through implementation, verification,
and the repository's pull request workflow. Do not stop at a diagnosis, a proposed
fix, or an offer to continue when the next action is already authorized.
Respect explicit limits: investigate only, explain only, review only, planning,
read-only work, and requests to pause or stop. Pure informational questions do not
authorize changes. Follow task-specific handoff requirements and permission boundaries;
this default does not authorize deployment, destructive actions, or unrelated work.
Follow directly related causes and regressions needed to resolve the original
problem; do not expand into an unbounded cleanup or the entire repository backlog.
When a required decision, approval, access, or evidence cannot be obtained yourself,
finish independent authorized work, explain the concrete blocker and recommended
next step, and use the available hand-back or decision mechanism. Do not label an
unresolved problem complete or no-change-needed merely because investigation ended.
Before finishing, check the original objective against what was actually changed
and verified. Continue if actionable in-scope work remains; otherwise report the
outcome and any limitations and use the appropriate completion signal.
"""

#: One clause per tool, deliberately thin: this paragraph is an index, not a spec (see
#: the module docstring). The tool's OWN description — sent with the tool schema, so it
#: is in context whenever the tool is mounted — is the single authority for the rules
#: (when to call it, what NOT to combine it with); a second, near-verbatim copy here is
#: the parallel-implementation anti-pattern this module exists to avoid, and it is
#: always the WEAKER copy that ends up in the system prompt (task-8 review, prod
#: fdc6b27a). Every name in `registry.mounted_names`'s possible output must have an
#: entry here — `test_session_tools_instruction.py` asserts coverage so a fourth
#: `ToolSpec` without one fails a test rather than crashing every Claude spawn.
_BLURBS: dict[str, str] = {
    "roadmap_needs_you": "list a product's roadmap items needing human attention.",
    "roadmap_item": "read the detail needed to discuss a roadmap item.",
    "roadmap_resolve": "record the user's chosen proposal resolution with tool approval.",
    "roadmap_hold": "hold or release a roadmap item on the user's behalf.",
    "roadmap_advance": "approve the initiative stage the user has reviewed.",
    "session_context": ("how you were dispatched (repo, branch, origin, goal), a "
                        "scratchDir for run logs that survives daemon restarts, and the "
                        "marker that keeps the Backlog Runner off issues you file for "
                        "your own plan."),
    "report_feedback": f"a problem with {brand.NAME} itself, never the project.",
    "mark_complete": "you have fully accomplished this session's objective.",
    "mark_no_pr": "the work is finished but no pull request is appropriate.",
    "hand_back": ("you need a decision, review, approval or information from the user "
                  "before the objective can be finished."),
    "set_hold": ("you want it on the record that this issue needs evidence, or a stronger "
                 "model, before anyone builds it."),
    "propose_decision": ("the work has hit a product choice only the user can make: "
                         "draft the options instead of guessing or handing back."),
    "share_artifact": ("you have a link, URL, local path or image the user must see — a "
                       "PR, a report, a screenshot — so it is never lost in prose or a "
                       "collapsed tool result."),
    "request_model": "you want to run the rest of the work on a different model tier.",
    "dispatch_session": "you need a linked child session the user can steer independently.",
    "reply_to_child": ("a child session handed back or stopped and you can answer, "
                       "resume or re-direct it yourself."),
}

#: The ONE cross-tool sentence the index may carry beyond naming tools: the
#: distinction between the three signals is not a rule of any single tool, so no
#: description can hold it alone (spec 2026-09-08-hand-back-signal-tool §7). Emitted
#: only when the signalling tools are mounted — it names them.
_THREE_WAYS_LINE = (
    "A turn ends one of three ways: `mark_complete`, `mark_no_pr`, or `hand_back`. "
    "Ending a turn is not completion, and a question with no `hand_back` alerts nobody."
)


def _session_tools_instruction(*, signalling_tools: bool, self_model_switch: bool,
                               child_sessions: bool = False) -> str:
    """The paragraph naming exactly the tools THIS session was mounted with.

    Built from ``registry.mounted_names`` — THE predicate ``build_session_tools`` and
    ``tools.mounted_tool_names`` also select from — so the list, and its count, can
    never drift from what was actually declared. This replaced a hand-typed "two MCP
    tools" that stopped being true the day a third tool was added.
    """
    names = mounted_names(signalling_tools=signalling_tools,
                          self_model_switch=self_model_switch,
                          child_sessions=child_sessions)
    lines = "\n".join(f"`{name}`: {_BLURBS[name]}" for name in names)
    tail = f"\n{_THREE_WAYS_LINE}" if signalling_tools else ""
    return (
        f"{brand.NAME} is the harness that dispatched and supervises this session; it "
        f"exposes {len(names)} MCP tools under the `{brand.MCP_SERVER_NAME}` server.\n"
        f"{lines}{tail}"
    )


def session_instructions(*, session_tools: bool, signalling_tools: bool,
                         self_model_switch: bool, child_sessions: bool = False) -> str:
    """THE one composition of "engine instructions + session-tools paragraph", called by
    both engines: ``claude_sdk_factory._build_append_string`` (the SDK's system-prompt
    append) and ``CodexAgentDriver.start``'s fresh ``thread/start``
    (``developerInstructions``). Two spellings of this join is how one engine would end
    up telling the model about tools it does not have — or, as happened first, not
    telling it about tools it does.

    A session is told exactly ONE way to signal completion / no-PR / a model request,
    never both and never neither:

    - ``signalling_tools`` → the tools paragraph names every mounted tool (including
      ``request_model`` only when ``self_model_switch``) and NO marker instruction is
      emitted at all — the tools cover what the markers used to.
    - otherwise → ``marker_instructions(...)`` exactly as before, plus the session-tools
      paragraph (naming only the two always-on READ tools) when ``session_tools``.
    - neither flag → ``marker_instructions(...)`` with the surface off entirely.

    Every path also receives the same problem-solving policy, even with tools off.

    Every argument is the caller's already-resolved answer, not a re-read of a flag: the
    Claude path knows them from ``session_tools_enabled`` / ``self_model_switch_enabled``
    and the Codex path from whether a ``CodexSessionTools`` was handed to the driver and
    from ``AgentCapabilities.durable_tool_surface``. They must not be able to disagree
    with what was actually mounted.
    """
    if not session_tools:
        instructions = marker_instructions(self_model_switch=self_model_switch)
    else:
        tools_paragraph = _session_tools_instruction(
            signalling_tools=signalling_tools, self_model_switch=self_model_switch,
            child_sessions=child_sessions)
        instructions = tools_paragraph if signalling_tools else (
            f"{marker_instructions(self_model_switch=self_model_switch)}\n\n{tools_paragraph}")
    return f"{instructions}\n\n{FOLLOW_THROUGH_INSTRUCTION}"


def finish_signal_instruction(*, signalling_tools: bool, mcp_namespaced: bool) -> str:
    """How a nudge tells THIS session to finish, matching what it was told at spawn.

    The same either/or as ``session_instructions``: a session on the signalling-tool
    surface finishes with ``mark_complete``; any other session was told only the
    completion marker and has no such tool. Autopilot's composer used to name
    ``mark_complete`` unconditionally, so a marker-only session was asked for a tool
    it could not see (#2928). ``mcp_namespaced`` is True for Claude, whose SDK names
    the tool ``mcp__<server>__mark_complete`` and may defer it behind ToolSearch;
    Codex declares it as a bare ``dynamicTools`` name.
    """
    if not signalling_tools:
        return f"end the final message with `{COMPLETION_MARKER}` on its own line"
    name = "mark_complete"
    if not mcp_namespaced:
        return f"call the `{name}` tool"
    return (f"call the `{brand.MCP_TOOL_PREFIX}{name}` tool (load it with ToolSearch "
            f"first if it is listed as deferred)")
