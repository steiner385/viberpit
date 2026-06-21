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
        return custom or auto
    except Exception:  # never raise into the session loop
        log.warning("read_ai_title failed for %s", claude_session_id, exc_info=True)
        return None
