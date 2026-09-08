"""THE one composition of "engine instructions + session-tools paragraph" — the single
point where a session's system prompt learns how to signal completion, no-PR, and a model
request, either through markers or through the signalling tools, never both.

Detail lives in the tool descriptions, which the model reads at call time; the paragraph
here is an index naming what is mounted, not a spec.

Both the product name and the MCP server name come from ``ccswitch.brand``: this
paragraph tells the model which server to look under, so a hand-typed name would
point it at a server that is not mounted."""
from __future__ import annotations

from ccswitch import brand

from ..completion import marker_instructions
from .registry import mounted_names

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
    "session_context": "how you were dispatched (repo, branch, origin, goal).",
    "report_feedback": f"a problem with {brand.NAME} itself, never the project.",
    "mark_complete": "you have fully accomplished this session's objective.",
    "mark_no_pr": "the work is finished but no pull request is appropriate.",
    "request_model": "you want to run the rest of the work on a different model tier.",
}


def _session_tools_instruction(*, signalling_tools: bool, self_model_switch: bool) -> str:
    """The paragraph naming exactly the tools THIS session was mounted with.

    Built from ``registry.mounted_names`` — THE predicate ``build_session_tools`` and
    ``tools.mounted_tool_names`` also select from — so the list, and its count, can
    never drift from what was actually declared. This replaced a hand-typed "two MCP
    tools" that stopped being true the day a third tool was added.
    """
    names = mounted_names(signalling_tools=signalling_tools,
                          self_model_switch=self_model_switch)
    lines = "\n".join(f"`{name}`: {_BLURBS[name]}" for name in names)
    return (
        f"{brand.NAME} is the harness that dispatched and supervises this session; it "
        f"exposes {len(names)} MCP tools under the `{brand.MCP_SERVER_NAME}` server.\n"
        f"{lines}"
    )


def session_instructions(*, session_tools: bool, signalling_tools: bool,
                         self_model_switch: bool) -> str:
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
    - neither flag → ``marker_instructions(...)`` alone, byte-identical to today's output
      with the surface off entirely.

    Every argument is the caller's already-resolved answer, not a re-read of a flag: the
    Claude path knows them from ``session_tools_enabled`` / ``self_model_switch_enabled``
    and the Codex path from whether a ``CodexSessionTools`` was handed to the driver and
    from ``AgentCapabilities.durable_tool_surface``. They must not be able to disagree
    with what was actually mounted.
    """
    if not session_tools:
        return marker_instructions(self_model_switch=self_model_switch)

    tools_paragraph = _session_tools_instruction(
        signalling_tools=signalling_tools, self_model_switch=self_model_switch)
    if signalling_tools:
        return tools_paragraph
    return f"{marker_instructions(self_model_switch=self_model_switch)}\n\n{tools_paragraph}"
