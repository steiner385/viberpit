"""Native vs virtual display routing — the decision, and nothing else.

A session's GUI goes to the runner host's real X display when the browser that
created it runs on that same host; otherwise to a pooled Xvfb display viewed over
VNC. This module is pure so the rule can be tested exhaustively and so every
caller (the orchestrator's spawn, resume and adopt paths) decides the same way.
Design: docs/superpowers/specs/2026-09-06-native-display-routing-design.md §4.2.
"""
from __future__ import annotations

from typing import NamedTuple

DISPLAY_NATIVE = "native"
DISPLAY_VIRTUAL = "virtual"
# Request-only value ("decide for me"); never persisted.
DISPLAY_AUTO = "auto"


class DisplayDecision(NamedTuple):
    """The routing decision plus, for a would-be-native session sent to a virtual
    display because the desktop is unavailable, WHY (else None)."""
    mode: str
    fallback_reason: str | None = None


def _is_local(client_node: str | None, runner: str) -> bool:
    """True iff the requesting client's tailnet node IS the runner host."""
    return bool(client_node and runner
                and client_node.strip().lower() == runner.strip().lower())


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
    # Normalise first, so the rules below only ever see one of the three constants.
    # None and anything this daemon does not recognise — a newer edge may send a
    # mode this build predates — collapse to DISPLAY_AUTO, "decide for me", which
    # is exactly the locality rule at the bottom. Behaviour is unchanged; this only
    # binds the request-only constant to the branch that consumes it.
    if requested not in (DISPLAY_NATIVE, DISPLAY_VIRTUAL):
        requested = DISPLAY_AUTO
    if requested == DISPLAY_VIRTUAL:
        return DISPLAY_VIRTUAL
    if not native_available:
        return DISPLAY_VIRTUAL
    if requested == DISPLAY_NATIVE:
        return DISPLAY_NATIVE
    if _is_local(client_node, runner):
        return DISPLAY_NATIVE
    return DISPLAY_VIRTUAL


# D-18 dev-server preview: where "Open" sends THIS viewer, decided per request from the
# requesting client's tailnet node — the same locality rule as the display decision.
PREVIEW_LOCAL = "local-link"
PREVIEW_TAILNET = "tailnet-link"
PREVIEW_VNC = "vnc"


def resolve_preview_route(
    *,
    client_node: str | None,
    runner: str,
    exposed: bool,
    tailnet_reachable: bool,
) -> str:
    """PREVIEW_LOCAL, PREVIEW_TAILNET or PREVIEW_VNC for one dev server and one viewer.

    Rules, in order: a viewer whose tailnet node IS the session's host opens it in their
    own browser on loopback; a server bound to every address (``exposed``) on a host with
    a known tailnet name opens over the tailnet; anything else opens in the session's
    virtual display. ``client_node`` must come from the verified peer
    (``identity.client_node_name``), never from anything the client sends.
    """
    if _is_local(client_node, runner):
        return PREVIEW_LOCAL
    if exposed and tailnet_reachable:
        return PREVIEW_TAILNET
    return PREVIEW_VNC


def native_fallback(
    *,
    requested: str | None,
    client_node: str | None,
    runner: str,
    native_available: bool,
) -> bool:
    """True iff this session WOULD have gone native but the desktop is unavailable.

    The caller then records why (``display_fallback_reason``) so the UI can explain
    the silent virtual routing. An explicit "virtual" request is never a fallback.
    """
    if native_available or requested == DISPLAY_VIRTUAL:
        return False
    return requested == DISPLAY_NATIVE or _is_local(client_node, runner)
