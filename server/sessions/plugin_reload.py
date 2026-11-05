"""Pure helpers for the ``reload_plugins`` control request (D-16c residuals, #3314).

The SDK has no method for it, so ``ClaudeAgentDriver.reload_plugins`` sends it raw on the
control channel (the seam ``apply_flag_settings`` uses). This module owns the request
shape, the refusal codes and the reduction of the CLI's reply.
"""
from __future__ import annotations

RELOAD_UNAVAILABLE = "RELOAD_UNAVAILABLE"
RELOAD_BUSY = "RELOAD_BUSY"
RELOAD_REFUSED = "RELOAD_REFUSED"
RELOAD_FAILED = "RELOAD_FAILED"
RELOAD_CONTROL_TIMEOUT_S = 60.0


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
