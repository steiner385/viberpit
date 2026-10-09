"""Read Claude's own session title from the transcript JSONL.

Claude records ``customTitle`` (user-set) and ``ai-title`` (model-generated) in the
per-session transcript. We reuse that rather than summarising ourselves. Any I/O or
parse error degrades to ``None`` — this must never break the session loop.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from .transcript_entries import is_harness_user_entry

log = logging.getLogger(__name__)

_CUSTOM_KEYS = ("customTitle",)
_AUTO_KEYS = ("ai-title", "aiTitle", "summary")
_TITLE_KEYS = _CUSTOM_KEYS + _AUTO_KEYS


def _scan_lines(lines: Iterable[str]) -> tuple[str | None, str | None]:
    """Scan transcript JSONL lines; return (customTitle, autoTitle) — last non-empty wins.

    Takes an iterable (an open file) rather than the file's text (#2885): a
    ``read_text()`` + ``splitlines()`` of a long transcript held two full copies of
    it — ~180 MB for the 91 MB one measured on dobby — in a daemon worker thread."""
    custom: str | None = None
    auto: str | None = None
    for line in lines:
        line = line.strip()
        # Most lines are multi-MB tool results that name no title key. json.loads
        # holds the GIL, so parsing them from a worker thread still starves the
        # event loop (#2144 stall dumps); a substring test is C-speed and exact.
        if not line or not any(k in line for k in _TITLE_KEYS):
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
        with open(path, encoding="utf-8", errors="replace") as f:
            custom, auto = _scan_lines(f)
    except OSError:
        return None
    except Exception:  # never raise into the session loop
        log.warning("read_ai_title_from_file failed for %s", path, exc_info=True)
        return None
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
    prompts: list[str] = []
    try:
        # Streamed line by line, not read_text(): see _scan_lines (#2885).
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
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
    except OSError:
        return []
    except Exception:  # never raise into the session loop
        log.warning("read_user_prompts_from_file failed for %s", path, exc_info=True)
        return []
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
                with open(path, encoding="utf-8", errors="replace") as f:
                    c, a = _scan_lines(f)
            except OSError:
                continue
            if c:
                custom = c
            if a:
                auto = a
        return custom or auto
    except Exception:  # never raise into the session loop
        log.warning("read_ai_title failed for %s", engine_session_id, exc_info=True)
        return None
