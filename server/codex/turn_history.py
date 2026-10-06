"""ViberPit-owned completed Codex turns, independent of native history retention.

Only normalized transcript events are stored. This is an account-local sidecar,
not a reader or writer of Codex's private session files. A bad record never makes
part of a turn replayable; a torn append cannot hide earlier complete turns.
"""
from __future__ import annotations

import json
import logging
import math
import os
import types
from collections.abc import Sequence
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import get_args, get_origin, get_type_hints, is_typeddict

from server.assistant.context import PromptContext
from server.codex.item_times import thread_sidecar_path
from server.sessions import events as ev
from server.sessions.prompt_origin import PromptOrigin

log = logging.getLogger(__name__)

# The same vocabulary drives live buffering and durable replay. Control events
# must be re-derived from current state rather than resurrected by a journal.
_EVENT_TYPES = {
    'user': ev.UserMessage,
    'turnStart': ev.TurnStart,
    'turnModel': ev.TurnModel,
    'sessionMeta': ev.SessionMeta,
    'textDelta': ev.TextDelta,
    'thinkingDelta': ev.ThinkingDelta,
    'toolCall': ev.ToolCall,
    'toolResult': ev.ToolResult,
    'turnEnd': ev.TurnEnd,
    'error': ev.ErrorEvent,
}
TRANSCRIPT_EVENT_TYPES = tuple(_EVENT_TYPES.values())


def _matches(value: object, annotation: object) -> bool:
    """Validate JSON field shapes against the canonical dataclass annotations."""
    origin = get_origin(annotation)
    if origin is types.UnionType:
        return any(_matches(value, item) for item in get_args(annotation))
    if annotation is type(None):
        return value is None
    if annotation is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    if annotation in (str, int, bool, dict):
        return type(value) is annotation
    if is_typeddict(annotation):
        return isinstance(value, dict)
    if origin is list:
        return isinstance(value, list) and all(
            _matches(item, get_args(annotation)[0]) for item in value
        )
    if annotation is ev.ToolKind:
        return isinstance(value, ev.ToolKind)
    return False


def decode_event(wire: dict) -> object:
    """Decode a whitelisted normalized wire event; reject invalid known fields.

    Transport-only envelope fields are ignored. No module/class name from disk
    is ever imported or executed. Missing timestamps remain unknown, not 'now'.
    """
    kind = wire.get('type')
    if not isinstance(kind, str):
        raise ValueError('invalid transcript event type')
    cls = _EVENT_TYPES.get(kind)
    if cls is None:
        raise ValueError('unsupported transcript event')
    hints = get_type_hints(cls, localns={
        'PromptContext': PromptContext, 'PromptOrigin': PromptOrigin,
    })
    kwargs: dict[str, object] = {}
    for field in fields(cls):
        # These are intentionally excluded from to_wire and must stay internal.
        if field.name in ('raw_message', 'pr_resolution_note'):
            continue
        parts = field.name.split('_')
        key = parts[0] + ''.join(part.title() for part in parts[1:])
        if cls is ev.ToolCall and field.name == 'kind':
            key = 'toolKind'
        if key not in wire:
            if field.default is MISSING and field.default_factory is MISSING:
                raise ValueError('missing transcript event field')
            if field.name == 'ts':
                kwargs[field.name] = None
            continue
        value = wire[key]
        if cls is ev.ToolCall and field.name == 'kind':
            try:
                value = ev.ToolKind(value)
            except (TypeError, ValueError):
                raise ValueError('invalid transcript tool kind') from None
        if not _matches(value, hints[field.name]):
            raise ValueError('invalid transcript event field')
        kwargs[field.name] = value
    return cls(**kwargs)


@dataclass(frozen=True)
class RecordedTurn:
    turn_id: str
    events: list[object]


def _decode_turn(record: object, thread_id: str) -> RecordedTurn:
    if not isinstance(record, dict) or record.get('version') != 1:
        raise ValueError('invalid transcript record')
    turn_id = record.get('turnId')
    if record.get('threadId') != thread_id or not isinstance(turn_id, str) or not turn_id:
        raise ValueError('invalid transcript identity')
    wires = record.get('events')
    if not isinstance(wires, list) or not all(isinstance(w, dict) for w in wires):
        raise ValueError('invalid transcript events')
    events = [decode_event(w) for w in wires]
    roots = [e for e in events if isinstance(e, (ev.TurnStart, ev.TurnEnd))
             and e.parent_tool_use_id is None]
    if (len(roots) != 2 or not isinstance(roots[0], ev.TurnStart)
            or not isinstance(roots[1], ev.TurnEnd) or events[-1] is not roots[1]):
        raise ValueError('incomplete transcript turn')
    for event in events:
        event_turn = getattr(event, 'turn_id', None)
        if (not isinstance(event, ev.UserMessage)
                and event_turn is not None and event_turn != turn_id
                and getattr(event, 'parent_tool_use_id', None) is None):
            raise ValueError('mismatched transcript turn')
    return RecordedTurn(turn_id, events)


class TurnHistoryStore:
    """Private append-only completed turns under the account's sidecar root.

    All I/O failures are best-effort and logged without paths or event contents.
    Failed writes are retried on the next turn (e.g. after disk space is freed).
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def record(self, thread_id: str, turn_id: str, events: Sequence[object]) -> None:
        path = thread_sidecar_path(self._root, thread_id, '.turns.jsonl')
        if path is None or thread_id.endswith('\n'):
            log.warning('codex.turn_history: refused unsafe thread id')
            return
        try:
            if not all(type(event) in TRANSCRIPT_EVENT_TYPES for event in events):
                raise ValueError('unsupported transcript event')
            record = {'version': 1, 'threadId': thread_id, 'turnId': turn_id,
                      'events': [event.to_wire() for event in events]}  # type: ignore[attr-defined]
            _decode_turn(record, thread_id)
            # A leading newline separates the next valid record from a torn tail.
            payload = ('\n' + json.dumps(record, separators=(',', ':'), allow_nan=False)
                       + '\n').encode('utf-8')
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._root.chmod(0o700)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                if os.write(fd, payload) != len(payload):
                    raise OSError('short transcript append')
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            log.warning('codex.turn_history: turn not persisted (%s)', type(exc).__name__)

    def delete(self, thread_id: str) -> None:
        """Remove a permanently deleted thread's content without following links."""
        path = thread_sidecar_path(self._root, thread_id, '.turns.jsonl')
        if path is None or thread_id.endswith('\n'):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning('codex.turn_history: history not deleted (%s)', type(exc).__name__)

    def load(self, thread_id: str) -> list[RecordedTurn]:
        path = thread_sidecar_path(self._root, thread_id, '.turns.jsonl')
        if path is None or thread_id.endswith('\n'):
            return []
        turns: dict[str, RecordedTurn] = {}
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        turn = _decode_turn(json.loads(line), thread_id)
                    except (ValueError, TypeError):
                        log.warning('codex.turn_history: ignored invalid turn record')
                        continue
                    turns[turn.turn_id] = turn
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning('codex.turn_history: history unreadable (%s)', type(exc).__name__)
        return list(turns.values())
