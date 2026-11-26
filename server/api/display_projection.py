"""Minimized payloads for the display (kiosk) principal (#1408).

An unattended wall display is read by whoever walks past, so the pipeline feed it is
served drops what identifies a person or a host: email-shaped identities, tailnet
hostnames, and the free text of a session's goal/prompt. Applied at the ENDPOINT, never
in the client — a display credential can call the API directly, so a client-side hide
would hide nothing.

It is a deny-by-shape scrub over the serialized frame rather than a field allowlist, so
a field added to the frame later is still scrubbed by value even when nobody updated
this module (the test scans the serialized body against fixtures that hold all three
kinds in every position).
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Request

# Keys whose VALUE is free text a person wrote or an identity, whatever the value looks like.
_DROP_KEYS = frozenset({
    "goal", "prompt", "firstPrompt", "lastPrompt", "initialPrompt",
    "identity", "email", "account", "accountLabel", "hostname", "host",
    "machine", "cwd",
})
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TAILNET_HOST = re.compile(r"[\w-]+(?:\.[\w-]+)*\.ts\.net\b", re.IGNORECASE)
REDACTED = "[hidden]"


def is_display(request: Request) -> bool:
    return getattr(request.state, "scope", None) == "display"


def _scrub_str(value: str) -> str:
    return _TAILNET_HOST.sub(REDACTED, _EMAIL.sub(REDACTED, value))


def project_for_display(payload: Any) -> Any:
    """A copy of *payload* with identities, hostnames and goal/prompt text removed."""
    if isinstance(payload, dict):
        return {k: project_for_display(v) for k, v in payload.items() if k not in _DROP_KEYS}
    if isinstance(payload, list):
        return [project_for_display(v) for v in payload]
    if isinstance(payload, str):
        return _scrub_str(payload)
    return payload
