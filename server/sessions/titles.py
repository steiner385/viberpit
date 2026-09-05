"""Read Claude's own session title from the transcript JSONL.

Claude records ``customTitle`` (user-set) and ``ai-title`` (model-generated) in the
per-session transcript. We reuse that rather than summarising ourselves. Any I/O or
parse error degrades to ``None`` — this must never break the session loop.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .transcript_entries import is_harness_user_entry

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
    """Return the ordered, non-empty HUMAN prompts from a transcript file.

    Excludes assistant messages, tool-result echoes, and the CLI's own ``type:"user"``
    bookkeeping (``isMeta`` context, the ``/model`` local-command records a
    ``set_model`` journals, interrupt markers — see ``transcript_entries``): the
    fallback titler summarises these, so a session whose only "prompts" were the
    ``/model fable`` records got named "Set model to Fable". Never raises: returns
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
        if prompt and not is_harness_user_entry(obj, prompt):
            prompts.append(prompt)
    return prompts


def read_ai_title(projects_root: str | Path, engine_session_id: str) -> str | None:
    """Return the best available title for a session, or ``None``.

    Prefers a user-set ``customTitle`` over a model ``ai-title``/``summary``. Scans
    every JSONL line of every transcript matching the session id; the last non-empty
    value of each kind wins. Never raises.
    """
    if not engine_session_id:
        return None
    try:
        root = Path(projects_root)
        matches = list(root.glob(f"**/{engine_session_id}.jsonl"))
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
        log.warning("read_ai_title failed for %s", engine_session_id, exc_info=True)
        return None
