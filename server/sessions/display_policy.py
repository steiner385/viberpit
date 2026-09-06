"""Native vs virtual display routing — the decision, and nothing else.

A session's GUI goes to the runner host's real X display when the browser that
created it runs on that same host; otherwise to a pooled Xvfb display viewed over
VNC. This module is pure so the rule can be tested exhaustively and so every
caller (the orchestrator's spawn, resume and adopt paths) decides the same way.
Design: docs/superpowers/specs/2026-09-06-native-display-routing-design.md §4.2.
"""
from __future__ import annotations

DISPLAY_NATIVE = "native"
DISPLAY_VIRTUAL = "virtual"
# Request-only value ("decide for me"); never persisted.
DISPLAY_AUTO = "auto"


def resolve_display_mode(
    *,
    requested: str | None,
    client_node: str | None,
    runner: str,
    native_available: bool,
) -> str:
    """Return DISPLAY_NATIVE or DISPLAY_VIRTUAL.

    Rules, in order: an explicit "virtual" always wins; no native display means
    virtual; an explicit "native" needs no locality proof; otherwise native iff the
    requesting client's tailnet node IS the runner host. Anything unrecognised in
    ``requested`` (the API validates it, but daemons can be behind) reads as auto.
    """
    if requested == DISPLAY_VIRTUAL:
        return DISPLAY_VIRTUAL
    if not native_available:
        return DISPLAY_VIRTUAL
    if requested == DISPLAY_NATIVE:
        return DISPLAY_NATIVE
    if client_node and runner and client_node.strip().lower() == runner.strip().lower():
        return DISPLAY_NATIVE
    return DISPLAY_VIRTUAL
