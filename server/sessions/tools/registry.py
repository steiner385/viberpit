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

register_surface("session", SESSION_TOOL_SPECS)
