"""Extract the user's prompts from a Claude transcript JSONL.

These prompts are the highest-signal, lowest-token input for the AI title
generator (see ``title_generator.py``). Any I/O or parse error degrades to an
empty list — this must never break the session loop.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _prompt_text(message: dict) -> str:
    """Return the joined text of a transcript ``user`` message, or ``""``.

    Claude transcripts store ``content`` either as a plain string (common for
    user turns) or as a list of content blocks. Tool-result echoes are also
    typed ``user`` but carry no text block, so they naturally yield ``""``.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "".join(parts).strip()
    return ""


def read_user_prompts_from_file(path: str | Path) -> list[str]:
    """Return the ordered, non-empty user prompts from a transcript file.

    Excludes assistant messages and tool-result echoes. Never raises: returns
    ``[]`` for a missing/unreadable/garbage file.
    """
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    except Exception:  # never raise into the session loop
        log.warning("read_user_prompts_from_file failed for %s", path, exc_info=True)
        return []

    prompts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "user":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        prompt = _prompt_text(message)
        if prompt:
            prompts.append(prompt)
    return prompts
