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
from .registry import mounted_signal_names

#: The two READ tools every session with the surface mounted gets, regardless of engine
#: or capability — unlike the SIGNAL tools, these are never gated on `signalling_tools`.
_ALWAYS_ON_BLURBS: tuple[tuple[str, str], ...] = (
    ("session_context",
     "how you were dispatched (repo, branch, originating automation and issue, goal)"),
    ("report_feedback",
     f"a problem with {brand.NAME} ITSELF — contradictory or wrong instructions, an "
     "ambiguous dispatch prompt, an interrupt or model/account switch at a bad moment, a "
     "broken worktree, credential or tool it provides — never a bug in the project you are "
     "working on or a choice the user made. One report per distinct problem, at most three "
     "per session. A report never waits for a reply: keep working."),
)

#: What each SIGNAL tool replaces, told as an instruction "INSTEAD of a marker" — a
#: session with these mounted is never also taught the marker spelling for the same signal.
_SIGNAL_BLURBS: dict[str, str] = {
    "mark_complete":
        "you have FULLY accomplished the objective of this session. Call it only when "
        "the work is genuinely complete — never for an interim step, never while you are "
        "still working or waiting on something, and never in the same turn as a question "
        "to the user: if you need an answer, ask and do not call this.",
    "mark_no_pr":
        "the work is genuinely finished but NO pull request is appropriate — the objective "
        "is already satisfied without a code change, or the work was cancelled. Call it "
        "INSTEAD of mark_complete.",
    "request_model":
        "the work has turned out materially harder or easier than it looked and you want "
        "to run the rest of it on a different model tier.",
}


def _session_tools_instruction(*, signalling_tools: bool, self_model_switch: bool) -> str:
    """The paragraph naming exactly the tools THIS session was mounted with.

    Built from ``mounted_signal_names`` — the same predicate ``build_session_tools`` and
    ``tools.mounted_tool_names`` select from — so the list, and its count, can never drift
    from what was actually declared. This replaced a hand-typed "two MCP tools" that
    stopped being true the day a third tool was added.
    """
    names = ("session_context", "report_feedback") + mounted_signal_names(
        signalling_tools=signalling_tools, self_model_switch=self_model_switch)
    blurbs = dict(_ALWAYS_ON_BLURBS)
    blurbs.update(_SIGNAL_BLURBS)
    plural = "tool" if len(names) == 1 else "tools"
    lines = "\n".join(f"`{name}`: {blurbs[name]}" for name in names)
    return (
        f"{brand.NAME} is the harness that dispatched and supervises this session; it "
        f"exposes {len(names)} MCP {plural} under the `{brand.MCP_SERVER_NAME}` server.\n"
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
