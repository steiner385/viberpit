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
    ToolSpec("hand_back", ToolClass.SIGNAL, "Hand this session back to the user"),
    ToolSpec("request_model", ToolClass.SIGNAL, "Request a different model"),
    # SIGNAL for the same reason as request_model: it moves a roadmap chip, never a
    # card — an unattended runner session must be able to say "this needs evidence"
    # or "this needs a stronger model" (spec 2026-09-18 §8) without a prompt nobody
    # is there to answer. Agents may only set the two auto-exiting kinds; holds.py
    # enforces that, not this table.
    ToolSpec("set_hold", ToolClass.SIGNAL, "Hold or release this session's issue"),
    # MUTATION: it creates a session the user sees on the board. Gated on its
    # annotation by the permission layer, exactly like the assistant drawer's
    # create_session: our own MUTATION tools raise a card in EVERY session mode
    # (full-auto included) until the user grants "always allow" — see
    # PermissionRegistry.request's is_mcp_tool branch. Mounted only on the durable
    # surface with the kill switch on (child-session spec §4.2).
    ToolSpec("dispatch_session", ToolClass.MUTATION, "Start a child session"),
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
    of ``mounted_names`` below. ``hand_back`` rides with the two finishing signals: it
    is the third way a turn ends, and a session told about completion but not about
    hand-back is exactly the session that misuses ``mark_complete``. ``set_hold`` rides
    with them unconditionally too — it needs no engine capability beyond the tool
    surface itself, unlike ``request_model`` below.

    ``request_model`` is named only when ``self_model_switch`` — declaring it otherwise
    offers a call whose result is silently dropped (no engine capability to honour it).
    """
    if not signalling_tools:
        return ()
    names = ["mark_complete", "mark_no_pr", "hand_back", "set_hold"]
    if self_model_switch:
        names.append("request_model")
    return tuple(names)


def mounted_names(*, signalling_tools: bool, self_model_switch: bool,
                  child_sessions: bool = False) -> tuple[str, ...]:
    """THE full predicate for which tool names a session gets, in declaration order —
    READ_TOOL_NAMES (unconditional) followed by the SIGNAL half. ``tools.mounted_tool_names``
    (the ctx-unpacking adapter used by ``build_session_tools``) and ``tools/instruction.py``'s
    system-prompt paragraph both call this SAME function rather than each re-deriving "which
    tools does this session get": two copies of that condition is how a session ends up
    told about a tool it does not have, or declared one it was never told about.
    """
    names = READ_TOOL_NAMES + mounted_signal_names(
        signalling_tools=signalling_tools, self_model_switch=self_model_switch)
    # `dispatch_session` rides only on the durable (Claude) surface — Codex loses its
    # declared tools on resume and has no marker for a dispatch — and only while the
    # kill switch (``child_sessions``) is on.
    if signalling_tools and child_sessions:
        names = names + ("dispatch_session",)
    return names


register_surface("session", SESSION_TOOL_SPECS)
