"""Native / remote / virtual display routing — the decision, and nothing else.

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
# The session draws on another host's desktop through the display bridge.
DISPLAY_REMOTE = "remote"
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


def resolve_display_decision(
    *,
    requested: str | None,
    client_node: str | None,
    runner: str,
    native_available: bool,
    native_unavailable_reason: str | None,
    remote_target: str | None,
    remote_available: bool,
    remote_unavailable_reason: str | None,
) -> DisplayDecision:
    """The one place the display mode (and any fallback reason) is decided.

    Rules, in order: an explicit "virtual" always wins; an explicit "native" with the
    native display available is native; auto with the client local to the runner and
    native available is native; a host display target (``remote_target``) whose bridge
    is available is remote; anything else is virtual. A would-be-native session sent
    virtual carries ``native_unavailable_reason``; a would-be-remote one carries
    ``remote_unavailable_reason`` (which wins when both were wanted). Anything
    unrecognised in ``requested`` — including "remote": a client cannot force remote,
    only the host's configured target can — reads as auto.
    """
    # Normalise first, so the rules below only ever see one of the three constants.
    if requested not in (DISPLAY_NATIVE, DISPLAY_VIRTUAL):
        requested = DISPLAY_AUTO
    if requested == DISPLAY_VIRTUAL:
        return DisplayDecision(DISPLAY_VIRTUAL)
    wanted_native = requested == DISPLAY_NATIVE or _is_local(client_node, runner)
    if wanted_native and native_available:
        return DisplayDecision(DISPLAY_NATIVE)
    if remote_target:
        if remote_available:
            return DisplayDecision(DISPLAY_REMOTE)
        return DisplayDecision(
            DISPLAY_VIRTUAL,
            remote_unavailable_reason or (native_unavailable_reason if wanted_native else None))
    return DisplayDecision(DISPLAY_VIRTUAL, native_unavailable_reason if wanted_native else None)


# D-18 dev-server preview: where "Open" sends THIS viewer, decided per request from the
# requesting client's tailnet node — the same locality rule as the display decision.
PREVIEW_LOCAL = "local-link"
PREVIEW_TAILNET = "tailnet-link"
PREVIEW_VNC = "vnc"
#: No way to open it for this viewer: the session's host has no display for ``vnc``.
PREVIEW_NONE = "none"


def resolve_preview_route(
    *,
    client_node: str | None,
    runner: str,
    exposed: bool,
    tailnet_reachable: bool,
    vnc_available: bool = True,
) -> str:
    """PREVIEW_LOCAL, PREVIEW_TAILNET, PREVIEW_VNC or PREVIEW_NONE for one dev server and
    one viewer.

    Rules, in order: a viewer whose tailnet node IS the session's host opens it in their
    own browser on loopback; a server bound to every address (``exposed``) on a host with
    a known tailnet name opens over the tailnet; anything else opens in the session's
    virtual display — or nowhere (PREVIEW_NONE) when that host has no X11
    (``vnc_available``), since that route would always refuse. ``client_node`` must come
    from the verified peer (``identity.client_node_name``), never from anything the
    client sends.
    """
    if _is_local(client_node, runner):
        return PREVIEW_LOCAL
    if exposed and tailnet_reachable:
        return PREVIEW_TAILNET
    return PREVIEW_VNC if vnc_available else PREVIEW_NONE
