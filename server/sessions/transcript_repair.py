"""Heal a transcript left inconsistent by an interrupt mid-tool-call.

When a turn is interrupted while a tool_use is in flight — the CLI's "interrupted
before running" window, or a drain cancelled by a deploy/restart — the synthetic
``[Request interrupted by user for tool use]`` tool_result never gets written. The
conversation tail is then an assistant ``tool_use`` with no matching
``tool_result`` (an ORPHANED tool_use). On resume the ``claude`` binary rejects
that terminal state with ``error_during_execution`` and the diagnostic
``[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use``,
and every subsequent turn dead-ends the same way.

This module re-writes the missing tool_result — byte-compatible with the entry the
CLI writes itself in the normal interrupt path — so the resumed transcript is
well-formed again. We only ever repair the TRAILING orphan (a tool_result must
immediately follow its tool_use); a mid-conversation orphan is left alone.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import uuid as _uuid

log = logging.getLogger(__name__)

INTERRUPT_TEXT = "[Request interrupted by user for tool use]"


def _content_blocks(entry: dict) -> list:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def _tool_use_ids(entry: dict) -> list[str]:
    out: list[str] = []
    for b in _content_blocks(entry):
        if isinstance(b, dict) and b.get("type") == "tool_use":
            tid = b.get("id")
            if tid:
                out.append(str(tid))
    return out


def _tool_result_ids(entry: dict) -> set[str]:
    out: set[str] = set()
    for b in _content_blocks(entry):
        if isinstance(b, dict) and b.get("type") == "tool_result":
            tid = b.get("tool_use_id")
            if tid:
                out.add(str(tid))
    return out


def plan_repair(entries: list[dict], *, uuid_factory=None, now_iso: str) -> list[dict]:
    """Return synthetic tool_result entries to append, or [] if the tail is clean.

    Repairs only a TRAILING orphan: the last main-thread message must be an
    assistant ``tool_use`` whose ids have no matching ``tool_result`` after it.
    """
    uuid_factory = uuid_factory or (lambda: _uuid.uuid4().hex)
    # Mirror the binary's terminal-state check (S_o): it looks at the last
    # assistant/user MESSAGE, ignoring the marker entries (last-prompt, mode,
    # pr-link, attachment, summary, …) a real transcript appends after a turn.
    convo = [e for e in entries
             if isinstance(e, dict) and not e.get("isSidechain")
             and e.get("type") in ("assistant", "user")]
    if not convo:
        return []

    anchor = convo[-1]
    if anchor.get("type") != "assistant":
        return []   # conversation moved past any tool_use; nothing to heal
    orphan_ids = _tool_use_ids(anchor)
    if not orphan_ids:
        return []
    # Defensive: a tool_use already answered within the same trailing message.
    answered = _tool_result_ids(anchor)
    orphan_ids = [tid for tid in orphan_ids if tid not in answered]
    if not orphan_ids:
        return []

    anchor_uuid = anchor.get("uuid")
    synthetic = {
        "parentUuid": anchor_uuid,
        "isSidechain": False,
        "promptId": anchor.get("promptId") or anchor.get("requestId") or uuid_factory(),
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": INTERRUPT_TEXT,
                         "is_error": True, "tool_use_id": tid} for tid in orphan_ids],
        },
        "uuid": uuid_factory(),
        "timestamp": now_iso,
        "toolUseResult": f"Error: {INTERRUPT_TEXT}",
        "sourceToolAssistantUUID": anchor_uuid,
        "userType": "external",
        "entrypoint": "sdk-py",
    }
    # Carry the conversation's identity fields verbatim so resume accepts the entry.
    for key in ("cwd", "sessionId", "version", "gitBranch"):
        if key in anchor:
            synthetic[key] = anchor[key]
    return [synthetic]


def _read_entries(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        f = open(path, errors="ignore")
    except OSError:
        return out
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                out.append(o)
    return out


def truncate_corrupt_tail(path: str) -> int:
    """Remove a single provably-corrupt TRAILING line (a kill mid-write leaves a
    partial JSONL line). Only the last non-empty physical line is checked; mid-file
    corruption is left alone (it's skipped on read, not a resume-blocker). Returns 1
    if a line was removed, else 0. Idempotent."""
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return 0
    last = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last = i
            break
    if last is None:
        return 0
    try:
        json.loads(lines[last].strip())
        return 0   # tail parses → nothing to do
    except json.JSONDecodeError:
        pass
    with open(path, "w") as f:
        f.writelines(lines[:last])
    log.info("transcript_repair: truncated corrupt trailing line in %s", path)
    return 1


def repair_transcript(path: str, *, now_iso: str | None = None) -> int:
    """Append synthetic tool_results to heal a trailing orphaned tool_use.

    Returns the number of entries appended (0 = nothing to do / file absent).
    Idempotent: a healed tail has a matching tool_result, so a re-run is a no-op.
    """
    if not os.path.isfile(path):
        return 0
    truncate_corrupt_tail(path)
    entries = _read_entries(path)
    if now_iso is None:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    plan = plan_repair(entries, now_iso=now_iso)
    if not plan:
        return 0
    with open(path, "a") as f:
        for entry in plan:
            f.write(json.dumps(entry) + "\n")
    log.info("transcript_repair: healed %d orphaned tool_use(s) in %s", len(plan), path)
    return len(plan)


def find_transcript(config_dir: str, session_id: str) -> str | None:
    """Locate ``<config_dir>/projects/*/<session_id>.jsonl`` (the file is named by
    the claude session id, so the project dir need not be re-derived)."""
    matches = glob.glob(os.path.join(config_dir, "projects", "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def repair_resumed_transcript(config_dir: str, session_id: str) -> int:
    """Heal the transcript for a session about to be resumed. Best-effort: a
    missing file or unreadable transcript returns 0 rather than raising."""
    if not config_dir or not session_id:
        return 0
    path = find_transcript(config_dir, session_id)
    if path is None:
        return 0
    return repair_transcript(path)
