"""Daemon RPC for the display bridge (server/sessions/display_bridge.py).

The edge asserts, per target daemon, the full set of source hosts whose sessions should
draw on this host's desktop. Mirrors x11_rpc.py: bridge knowledge stays out of the
generic transport (daemon.py), which only reads ``app.state.display_bridge`` for /info.
"""
from server.ipc.daemon import register_rpc


def register_display_bridge(app, bridge) -> None:
    """Register ``display_bridge_ensure`` against *bridge* (None when the kill switch is off)."""

    async def display_bridge_ensure(params: dict) -> dict:
        if bridge is None:
            return {"bridges": []}
        raw_sources = params.get("sources")
        raw_dests = params.get("sshDests")
        sources = [s for s in raw_sources if isinstance(s, str)] if isinstance(
            raw_sources, list) else []
        dests = {k: v for k, v in raw_dests.items()
                 if isinstance(k, str) and isinstance(v, str)} if isinstance(
            raw_dests, dict) else {}
        return {"bridges": await bridge.ensure(sources, dests)}

    register_rpc(app, "display_bridge_ensure", display_bridge_ensure)
