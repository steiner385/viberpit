"""Source-walking guard: every loop that sweeps sessions must skip assistant sessions.

The assistant is a session record like any other, so an unguarded sweep would title it,
summarise it, suggest next prompts for it, infer a goal for it, hand it to autopilot or
auto-archive it. `is_assistant()` (server/sessions/types.py) is the ONE predicate; these
tests fail loudly when a new sweep forgets it or when someone open-codes the literal.
"""
from __future__ import annotations

import inspect
import pathlib
import re

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import ASSISTANT_KIND

_REPO = pathlib.Path(__file__).resolve().parents[2]

# Orchestrator methods that iterate/act on session records on behalf of a background
# loop or an enrichment read. Each must consult `is_assistant`.
GUARDED_ORCH_METHODS = (
    "_active_session_count",        # capacity gate
    "_autopilot_list_governed",     # autopilot
    "_autopilot_fleet_snapshot",    # autopilot's fleet context
    "_titling_sweep",               # auto-titles
    "_goal_sweep",                  # inferred goals
    "latest_suggestions",           # next-prompt suggestions
    "latest_summary",               # one-line status summaries
    "_maybe_autoarchive_completed",  # auto-archive
    "_recover_undelivered_prompts",  # rehydrate's initial-prompt re-send
    "list_sessions",                # the dashboard list
)


def test_guarded_orchestrator_loops_call_is_assistant():
    missing = [name for name in GUARDED_ORCH_METHODS
               if "is_assistant(" not in inspect.getsource(
                   getattr(SessionOrchestrator, name))]
    assert not missing, f"session sweeps missing an is_assistant guard: {missing}"


def test_every_session_loop_skips_assistants():
    """Any `for <x> in <…>list_sessions(` in the automation/helper modules must be
    followed (within 6 lines) by an `is_assistant(` check."""
    roots = ["server/automations", "server/sessions/title_generator.py",
             "server/sessions/summaries.py", "server/sessions/suggestions.py",
             "server/sessions/goal_inference.py"]
    offenders = []
    for root in roots:
        base = _REPO / root
        for p in ([base] if root.endswith(".py") else base.rglob("*.py")):
            lines = p.read_text().splitlines()
            for i, ln in enumerate(lines):
                if re.search(r"for \w+ in .*list_sessions\(", ln):
                    window = "\n".join(lines[i:i + 7])
                    if "is_assistant(" not in window:
                        offenders.append(f"{p}:{i + 1}")
    assert not offenders, offenders


def test_assistant_kind_literal_is_not_open_coded():
    """`is_assistant` / `is_assistant_kind` / ASSISTANT_KIND are the only ways to ask
    "is this the assistant" — a bare == "assistant" comparison is the parallel
    implementation this task exists to avoid."""
    offenders = []
    pattern = re.compile(r'(kind\s*[=!]=\s*["\']assistant["\']'
                         r'|["\']assistant["\']\s*[=!]=\s*\w*kind)')
    for root in ("server", "src"):
        for p in (_REPO / root).rglob("*.py"):
            if "/tests/" in str(p) or p.name == "types.py":
                continue
            for i, ln in enumerate(p.read_text().splitlines()):
                if pattern.search(ln):
                    offenders.append(f"{p.relative_to(_REPO)}:{i + 1}: {ln.strip()}")
    assert not offenders, offenders


def test_is_assistant_is_duck_typed():
    """Fleet mirrors and DB rows must qualify, not just SessionRecord."""
    from server.sessions.types import is_assistant

    class Row:
        kind = ASSISTANT_KIND

    class Other:
        pass

    assert is_assistant(Row())
    assert not is_assistant(Other())
    assert not is_assistant(None)
