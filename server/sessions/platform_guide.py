"""The per-platform guide appended to a platform-routed session's instructions.

A session routed for ``windows`` (``Session.platform``) is told how to use the host's
Windows VM; a Linux session on the same host is told nothing. The text lives in a
file the operator installs (``docs/runbooks/windows-vm-session-guide.md`` ->
``/etc/viberpit/windows-guide.md``), read on every spawn so an edit applies to the
next one.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

#: Prompt text, so it is capped: a runaway file must not eat the context window.
MAX_GUIDE_BYTES: Final = 8 * 1024

#: platform -> the Settings attribute holding its guide file path.
_GUIDE_SETTINGS: Final = {"windows": "host_platform_guide_windows"}


def load_platform_guide(platform: str | None, settings) -> str | None:
    """The guide text for *platform*, or ``None`` (linux/unset/unknown platform, unset
    setting, missing or unreadable file). Never raises: a missing guide must not stop a
    session from spawning, so it logs at INFO, not WARNING (WARNING auto-files issues)."""
    attr = _GUIDE_SETTINGS.get(platform or "")
    path = getattr(settings, attr, None) if attr else None
    if not path:
        return None
    try:
        raw = Path(path).read_bytes()[:MAX_GUIDE_BYTES]
    except OSError as exc:
        log.info("platform guide for %s unavailable (%s): %s", platform, path, exc)
        return None
    text = raw.decode("utf-8", errors="ignore").strip()
    return text or None
