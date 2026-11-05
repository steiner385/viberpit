"""Pure helpers for the ``reload_plugins`` control request (D-16c residuals, #3314).

The SDK has no method for it, so ``ClaudeAgentDriver.reload_plugins`` sends it raw on the
control channel (the seam ``apply_flag_settings`` uses). This module owns the request
shape, the refusal codes and the reduction of the CLI's reply.
"""
from __future__ import annotations

from collections.abc import Awaitable

RELOAD_UNAVAILABLE = "RELOAD_UNAVAILABLE"
RELOAD_BUSY = "RELOAD_BUSY"
RELOAD_REFUSED = "RELOAD_REFUSED"
RELOAD_FAILED = "RELOAD_FAILED"
RELOAD_CONTROL_TIMEOUT_S = 60.0
#: The ``reload_plugins_live`` RPC deadline: one host reloads its sessions concurrently,
#: each bounded by ``RELOAD_CONTROL_TIMEOUT_S``, plus transport margin. The client's
#: default (~30 s) would turn a slow MCP-starting reload into ``host_timeout``.
RELOAD_RPC_TIMEOUT_S = RELOAD_CONTROL_TIMEOUT_S + 20.0


class ReloadError(Exception):
    """An expected reload refusal; ``code`` is one of the ``RELOAD_*`` constants."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def request(hold_on_cache_impact: bool) -> dict:
    return {"subtype": "reload_plugins", "hold_on_cache_impact": hold_on_cache_impact}


def outcome(reply: object) -> dict:
    """Reduce the CLI's reply; total over any input (a non-dict reads as applied)."""
    if not isinstance(reply, dict):
        return {"outcome": "applied", "plugins": [], "errorCount": 0}
    plugins = reply.get("plugins")
    names = [p["name"] for p in plugins
             if isinstance(p, dict) and isinstance(p.get("name"), str)] \
        if isinstance(plugins, list) else []
    errors = reply.get("error_count")
    out: dict = {"outcome": "held" if reply.get("held") is True else "applied",
                 "plugins": names,
                 "errorCount": errors if isinstance(errors, int) else 0}
    impact = reply.get("cache_impact")
    if isinstance(impact, dict):
        out["cacheImpact"] = impact
    return out


def _malformed(host: str) -> dict:
    return {"host": host, "outcome": "failed", "failureCode": "unparseable_result",
            "message": "Malformed host reply."}


async def host_rows(host: str, call: Awaitable[object]) -> list[dict]:
    """Await one host's ``reload_plugins_live`` and label it — the single mapping shared by
    the fleet fan-out, the in-process facade and a fleetless ``RemoteOrchestrator``. A host
    that cannot answer yields exactly one ``failed`` row (no ``sessionId``, see
    ``plugin_cli.host_failure``), never an exception. A reply that is not a list is one
    ``unparseable_result`` row; inside a list, each non-dict row becomes its own
    ``unparseable_result`` row and the well-formed rows are kept."""
    from server.library.plugin_cli import host_failure
    try:
        rows = await call
    except Exception as exc:  # noqa: BLE001 — one host must not block the rest
        return [host_failure(host, exc)]
    if not isinstance(rows, list):
        return [_malformed(host)]
    return [r if isinstance(r, dict) else _malformed(host) for r in rows]
