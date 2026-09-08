from __future__ import annotations

from ccswitch import brand
from server.mcp_spec import ToolClass, ToolSpec, register_surface

# READ for both: neither changes anything a user can see. `report_feedback` writes a
# row + audit + counter, exactly the assistant's `report_gap` precedent, so it raises
# no permission card in any mode (spec §3.2, §12).
SESSION_TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("session_context", ToolClass.READ, "Read this session's context"),
    ToolSpec("report_feedback", ToolClass.READ, f"Report a problem with {brand.NAME}"),
    # SIGNAL, not READ: each moves something the user sees (a status badge, a
    # completion-bar verdict, a pending model approval). They are declared here
    # UNCONDITIONALLY even though `build_session_tools` filters them per engine —
    # the permission layer resolves a tool by name across every surface, and a name
    # missing from this table fails open into the mutation branch.
    ToolSpec("mark_complete", ToolClass.SIGNAL, "Mark this session complete"),
    ToolSpec("mark_no_pr", ToolClass.SIGNAL, "Finish without a pull request"),
    ToolSpec("request_model", ToolClass.SIGNAL, "Request a different model"),
]}

#: THE set of signalling tool names, derived from the table above rather than
#: re-typed. Both transcript builders — the live ``sessions/translator.py`` and the
#: JSONL ``sessions/replay.py`` — key on it to suppress the ToolCall card for a
#: top-level signalling call, and the translator keys on it to read the signal. A
#: hand-written second copy is how those three readers would come to disagree about
#: which names are signals; deriving it means a fourth ``ToolClass.SIGNAL`` spec is
#: picked up by all of them at once.
SIGNAL_TOOL_NAMES: frozenset[str] = frozenset(
    name for name, spec in SESSION_TOOL_SPECS.items() if spec.tool_class is ToolClass.SIGNAL)

#: THE ordered set of READ tool names, derived the same way as ``SIGNAL_TOOL_NAMES``
#: rather than hand-typed at each call site. A fourth READ tool added to the table
#: above is picked up here — and therefore by ``mounted_names`` below — automatically.
READ_TOOL_NAMES: tuple[str, ...] = tuple(
    name for name, spec in SESSION_TOOL_SPECS.items() if spec.tool_class is ToolClass.READ)


def mounted_signal_names(*, signalling_tools: bool, self_model_switch: bool) -> tuple[str, ...]:
    """Which SIGNAL tool names are mounted, in declaration order — the signalling half
    of ``mounted_names`` below.

    ``request_model`` is named only when ``self_model_switch`` — declaring it otherwise
    offers a call whose result is silently dropped (no engine capability to honour it).
    """
    if not signalling_tools:
        return ()
    names = ["mark_complete", "mark_no_pr"]
    if self_model_switch:
        names.append("request_model")
    return tuple(names)


def mounted_names(*, signalling_tools: bool, self_model_switch: bool) -> tuple[str, ...]:
    """THE full predicate for which tool names a session gets, in declaration order —
    READ_TOOL_NAMES (unconditional) followed by the SIGNAL half. ``tools.mounted_tool_names``
    (the ctx-unpacking adapter used by ``build_session_tools``) and ``tools/instruction.py``'s
    system-prompt paragraph both call this SAME function rather than each re-deriving "which
    tools does this session get": two copies of that condition is how a session ends up
    told about a tool it does not have, or declared one it was never told about.
    """
    return READ_TOOL_NAMES + mounted_signal_names(
        signalling_tools=signalling_tools, self_model_switch=self_model_switch)


register_surface("session", SESSION_TOOL_SPECS)
