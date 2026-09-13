"""Shared safety boundary for operational harnesses that archive test sessions.

This client check cannot lock settings. Callers must require a dedicated disposable
edge, daemon and database with stable configuration; apparent headroom is unsafe.
"""
from __future__ import annotations

from typing import Protocol

import httpx


class RetentionStep(Protocol):
    def finish(self, ok: bool, detail: str = "") -> None: ...


async def retention_is_safe(http: httpx.AsyncClient, step: RetentionStep) -> bool:
    """Fail closed: archiving can permanently purge unrelated retained sessions."""
    try:
        response = await http.get("/api/settings")
        response.raise_for_status()
        settings = response.json()
    except (httpx.HTTPError, ValueError):
        step.finish(False, "cannot verify archive retention; no archive cleanup performed")
        return False
    safe = (
        isinstance(settings, dict)
        and "maxArchivedSessions" in settings
        and settings["maxArchivedSessions"] is None
    )
    step.finish(safe, "unlimited retention verified" if safe else
                "requires explicit maxArchivedSessions=null; no archive cleanup performed")
    return safe

