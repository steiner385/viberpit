"""Read Claude's own session title from the transcript JSONL.

Claude records ``customTitle`` (user-set) and ``ai-title`` (model-generated) in the
per-session transcript. We reuse that rather than summarising ourselves. Any I/O or
parse error degrades to ``None`` — this must never break the session loop.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CUSTOM_KEYS = ("customTitle",)
_AUTO_KEYS = ("ai-title", "aiTitle", "summary")


def _scan_text(text: str) -> tuple[str | None, str | None]:
    """Scan transcript JSONL text; return (customTitle, autoTitle) — last non-empty wins."""
    custom: str | None = None
    auto: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        for k in _CUSTOM_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                custom = v.strip()
        for k in _AUTO_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                auto = v.strip()
    return custom, auto


def read_ai_title_from_file(path: str | Path) -> str | None:
    """Read the best title from a single transcript file path, or ``None``. Never raises."""
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    except Exception:  # never raise into the session loop
        log.warning("read_ai_title_from_file failed for %s", path, exc_info=True)
        return None
    custom, auto = _scan_text(text)
    return custom or auto


def read_ai_title(projects_root: str | Path, claude_session_id: str) -> str | None:
    """Return the best available title for a session, or ``None``.

    Prefers a user-set ``customTitle`` over a model ``ai-title``/``summary``. Scans
    every JSONL line of every transcript matching the session id; the last non-empty
    value of each kind wins. Never raises.
    """
    if not claude_session_id:
        return None
    try:
        root = Path(projects_root)
        matches = list(root.glob(f"**/{claude_session_id}.jsonl"))
        if not matches:
            return None
        custom: str | None = None
        auto: str | None = None
        for path in matches:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            c, a = _scan_text(text)
            if c:
                custom = c
            if a:
                auto = a
        return custom or auto
    except Exception:  # never raise into the session loop
        log.warning("read_ai_title failed for %s", claude_session_id, exc_info=True)
        return None
