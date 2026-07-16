"""Claude Code hooks configuration — read/validate/write the shared settings.json
`hooks` block, plus a sandboxed dry-run. Enabled hooks live under settings["hooks"];
disabled ones are parked under settings["disabledHooks"] (same nested shape) — a key
Claude ignores, giving comment-out semantics without deleting.

Hooks run arbitrary shell on the host every session; validation is a light sanity
gate, NOT a security boundary. All writes are atomic (write_json_secure, 0600) and
self-healing: a failure after read restores the original bytes so settings.json is
never left corrupt."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from typing import Any

from ccswitch import paths
from ccswitch.creds import write_json_secure

from .scan import read_json_safe

HOOK_EVENTS: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "Notification",
        "Stop",
        "SubagentStop",
        "SessionStart",
        "SessionEnd",
        "PreCompact",
    }
)

# Stable ordering for list output: declared event order, then enabled before disabled.
_EVENT_ORDER = [
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "SessionStart",
    "SessionEnd",
    "PreCompact",
]

_SAMPLE_EVENT: dict[str, Any] = {
    "session_id": "dry-run",
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
}

_DRY_RUN_TIMEOUT_S = 10
_MAX_CAPTURE = 4000


class HooksValidationError(ValueError):
    """Raised when a hook row is malformed. Carries a human-readable message."""


def _settings_path():
    return paths.shared_base() / "settings.json"


def _flatten_block(block: Any, *, enabled: bool) -> list[dict[str, Any]]:
    """Flatten a settings hooks/disabledHooks block into flat rows. Each command
    entry becomes one row. Defensive against corrupt/unexpected shapes."""
    rows: list[dict[str, Any]] = []
    if not isinstance(block, dict):
        return rows
    for event in _EVENT_ORDER:
        groups = block.get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                matcher = None
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if not isinstance(command, str):
                    continue
                rows.append(
                    {
                        "event": event,
                        "matcher": matcher,
                        "command": command,
                        "enabled": enabled,
                    }
                )
    return rows


def list_hooks() -> dict[str, list[dict[str, Any]]]:
    """Return {"events": [...]} flattening enabled (hooks) + disabled (disabledHooks)
    rows. Never raises on read: a missing/corrupt file degrades to an empty list."""
    data = read_json_safe(_settings_path())
    if not isinstance(data, dict):
        data = {}
    rows = _flatten_block(data.get("hooks"), enabled=True)
    rows += _flatten_block(data.get("disabledHooks"), enabled=False)
    return {"events": rows}


def validate_hooks(rows: list[dict[str, Any]]) -> None:
    """Raise HooksValidationError on the first malformed row."""
    if not isinstance(rows, list):
        raise HooksValidationError("hooks payload must be a list of rows")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise HooksValidationError(f"row {i}: must be an object")
        event = row.get("event")
        if event not in HOOK_EVENTS:
            raise HooksValidationError(f"row {i}: unknown event {event!r}")
        command = row.get("command")
        if not isinstance(command, str) or not command.strip():
            raise HooksValidationError(f"row {i}: command is required and must be a non-empty string")
        matcher = row.get("matcher")
        if matcher is not None and not isinstance(matcher, str):
            raise HooksValidationError(f"row {i}: matcher must be a string or null")
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            raise HooksValidationError(f"row {i}: enabled must be a boolean")
        try:
            shlex.split(command)
        except ValueError:
            raise HooksValidationError(f"row {i}: unbalanced quotes / bad shell syntax") from None


def _build_block(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group flat rows into the nested {event: [{matcher, hooks:[...]}]} shape.
    Rows sharing event+matcher collapse into one matcher-group with multiple
    command entries. Preserves first-seen order within each event."""
    block: dict[str, list[dict[str, Any]]] = {}
    # (event, matcher-key) -> group dict, so repeated event+matcher merge.
    index: dict[tuple[str, Any], dict[str, Any]] = {}
    for row in rows:
        event = row["event"]
        matcher = row.get("matcher")
        key = (event, matcher)
        group = index.get(key)
        if group is None:
            group = {}
            if matcher is not None:
                group["matcher"] = matcher
            group["hooks"] = []
            index[key] = group
            block.setdefault(event, []).append(group)
        group["hooks"].append({"type": "command", "command": row["command"]})
    return block


def set_hooks(rows: list[dict[str, Any]]) -> None:
    """Validate (raising before any write), then rebuild settings["hooks"] and
    settings["disabledHooks"] from rows. All other keys are preserved. Atomic and
    self-healing: on any error after read the original file bytes are restored."""
    validate_hooks(rows)

    path = _settings_path()
    original_text: str | None = None
    if path.exists():
        try:
            original_text = path.read_text()
        except OSError:
            original_text = None

    data = read_json_safe(path)
    if not isinstance(data, dict):
        data = {}

    try:
        enabled_rows = [r for r in rows if r["enabled"]]
        disabled_rows = [r for r in rows if not r["enabled"]]

        enabled_block = _build_block(enabled_rows)
        disabled_block = _build_block(disabled_rows)

        if enabled_block:
            data["hooks"] = enabled_block
        else:
            data.pop("hooks", None)
        if disabled_block:
            data["disabledHooks"] = disabled_block
        else:
            data.pop("disabledHooks", None)

        write_json_secure(path, data)
    except BaseException:
        # Never leave a corrupt settings.json: restore the original bytes.
        if original_text is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original_text)
            except OSError:
                pass
        raise


def dry_run(command: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run `command` via /bin/sh -c with the event JSON on stdin, in a light
    sandbox (temp cwd, PATH+HOME env only, 10s timeout). Never raises: a bad
    command is captured as stderr/nonzero exit."""
    payload = json.dumps(event if event is not None else _SAMPLE_EVENT)
    sandbox_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tempfile.gettempdir()),
    }
    stdout = ""
    stderr = ""
    exit_code = -1
    timed_out = False
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            input=payload,
            capture_output=True,
            text=True,
            timeout=_DRY_RUN_TIMEOUT_S,
            cwd=tempfile.gettempdir(),
            env=sandbox_env,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout if isinstance(e.stdout, str) else "") or ""
        stderr = (e.stderr if isinstance(e.stderr, str) else "") or ""
        exit_code = -1
    except Exception as e:  # noqa: BLE001 — never raise from a dry-run
        stderr = f"{type(e).__name__}: {e}"
        exit_code = -1

    return {
        "stdout": stdout[:_MAX_CAPTURE],
        "stderr": stderr[:_MAX_CAPTURE],
        "exitCode": exit_code,
        "timedOut": timed_out,
        "command": command,
    }
