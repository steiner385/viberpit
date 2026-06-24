"""Scan a Claude transcript for *open* background tasks (Monitors / backgrounded
Bash|Task) and build the turn that relaunches them after a deploy restart.

Pure + side-effect-free: the transcript .jsonl is the durable source of truth for
both launches (assistant tool_use) and terminations (task-notification), so no live
observer or mirror table is needed (constitution Principle VI)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

_TUID = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
_STATUS = re.compile(r"<status>(.*?)</status>")
# completed/failed = the task finished on its own; killed = interrupted (we re-arm it).
_TERMINAL = {"completed", "failed"}

# Grace past a wakeup's scheduled fire time before we treat it as lost. A wakeup is
# an in-process timer with NO cross-restart re-arm: if the daemon was down when it
# should have fired, it never fires and no new turn is ever written. Once we are this
# far past its fire time with no later turn, it is gone — not pending. Covers harness
# firing latency, clock skew, and a brief restart window without prematurely freeing a
# wakeup that is about to fire (or that just fired, racing the new turn's write).
_WAKEUP_GRACE_SEC = 300.0

_GUARD = ("This task was interrupted by a server restart and may have partially "
          "completed. Before redoing work, check current state (e.g. `git status`, "
          "what already exists) and continue from where it left off rather than "
          "restarting blindly.")


@dataclass
class OpenBgTask:
    tool_use_id: str
    kind: str          # "monitor" | "task"
    name: str          # originating tool name (Monitor / Bash / Task)
    input: dict        # verbatim tool_use input — replayed to relaunch faithfully
    description: str

    def to_dict(self) -> dict:
        return {"tool_use_id": self.tool_use_id, "kind": self.kind,
                "name": self.name, "input": self.input, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> "OpenBgTask":
        return cls(tool_use_id=d.get("tool_use_id", ""), kind=d.get("kind", "task"),
                   name=d.get("name", ""), input=d.get("input") or {},
                   description=d.get("description", ""))


def _is_background_launch(name: str, inp: dict) -> bool:
    return name == "Monitor" or bool(inp.get("run_in_background"))


def scan_open_background_tasks(
    jsonl_path: str, *, treat_killed_as_open: bool = True
) -> list[OpenBgTask]:
    """Open background tasks (launched, not yet terminated) in a transcript.

    ``treat_killed_as_open`` distinguishes the two callers. Re-arm (the default)
    keeps a ``killed`` task "open" so it can be relaunched. ``is_parked`` passes
    False: a killed task is dead, not a live external wait — and it is re-armed
    under a *new* tool-use-id (nothing ever marks the old killed id
    completed/failed), so counting it as open would pin the session on
    ``waiting_external`` forever.
    """
    terminal = _TERMINAL if treat_killed_as_open else _TERMINAL | {"killed"}
    launches: dict[str, OpenBgTask] = {}
    terminated: set[str] = set()
    try:
        f = open(jsonl_path, errors="ignore")
    except OSError:
        return []
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict) or o.get("isSidechain"):
                continue
            if o.get("type") == "assistant":
                content = (o.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name") or ""
                        inp: dict = cast(dict, b.get("input")) if isinstance(b.get("input"), dict) else {}
                        tid = b.get("id")
                        if tid and _is_background_launch(name, inp):
                            launches[tid] = OpenBgTask(
                                tool_use_id=tid,
                                kind="monitor" if name == "Monitor" else "task",
                                name=name, input=inp,
                                description=str(inp.get("description") or ""))
            # Terminations: any entry whose content carries a task-notification with a
            # completed/failed status (queue-operation enqueue or the user delivery).
            content = o.get("content")
            if isinstance(content, str) and "<task-notification>" in content:
                ms, mt = _STATUS.search(content), _TUID.search(content)
                if ms and mt and ms.group(1) in terminal:
                    terminated.add(mt.group(1))
    open_tasks = [t for tid, t in launches.items() if tid not in terminated]
    open_tasks.reverse()   # newest-first (dict preserves file order)
    return open_tasks


def _iter_jsonl(jsonl_path: str):
    """Yield each well-formed, non-sidechain JSON object from a transcript .jsonl."""
    try:
        f = open(jsonl_path, errors="ignore")
    except OSError:
        return
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and not o.get("isSidechain"):
                yield o


def _assistant_tool_names(o: dict) -> list[str]:
    """Tool names invoked in an assistant message (empty for any other entry)."""
    if o.get("type") != "assistant":
        return []
    content = (o.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b.get("name") or "" for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def _wakeup_delay_seconds(o: dict) -> float | None:
    """``delaySeconds`` of a ScheduleWakeup in this assistant entry, else None."""
    if o.get("type") != "assistant":
        return None
    content = (o.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    for b in content:
        if (isinstance(b, dict) and b.get("type") == "tool_use"
                and b.get("name") == "ScheduleWakeup"):
            inp = b.get("input")
            raw = inp.get("delaySeconds") if isinstance(inp, dict) else None
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def _parse_ts(s) -> datetime | None:
    """Parse a transcript ISO8601 timestamp (``...Z``) to an aware UTC datetime."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def scan_pending_wakeup(jsonl_path: str, *, now: datetime | None = None) -> bool:
    """True iff the agent is parked on a self-scheduled wakeup it hasn't acted on yet.

    A ``ScheduleWakeup`` tool call arms exactly one future re-invocation; when it fires,
    a new ``user`` turn begins (in ``/loop`` dynamic mode an ``isMeta`` re-injection).
    So the wakeup is still *armed* iff the **last assistant turn** — the assistant
    messages since the most recent ``user`` entry — issued a ``ScheduleWakeup``. If a
    later turn ran without re-arming, the loop ended and nothing is pending.

    A wakeup is an in-process timer with NO cross-restart re-arm (unlike background
    tasks, which are relaunched from the ``pending_rearm`` snapshot). If the daemon was
    down when it should have fired, it never fires and no resetting ``user`` turn is
    written — so a naive "last turn armed it" check would report it pending forever,
    pinning the session on ``waiting_external``. Guard against that: an armed wakeup
    whose scheduled fire time (``armed_at + delaySeconds``) passed by more than
    ``_WAKEUP_GRACE_SEC`` is treated as lost, not pending.

    Pure given ``now`` (defaults to the current UTC time); side-effect-free otherwise.
    """
    armed_at: datetime | None = None
    delay: float | None = None
    for o in _iter_jsonl(jsonl_path):
        if o.get("type") == "user":
            armed_at, delay = None, None   # a fresh prompt / fired wakeup starts a new turn
        else:
            d = _wakeup_delay_seconds(o)
            if d is not None:
                armed_at, delay = _parse_ts(o.get("timestamp")), d   # current turn armed one
    if delay is None:
        return False
    # Armed. If we can time it and its fire moment is well past, the daemon missed it.
    if armed_at is not None:
        now = now or datetime.now(timezone.utc)
        if now > armed_at + timedelta(seconds=delay + _WAKEUP_GRACE_SEC):
            return False
    return True


def is_parked(jsonl_path: str | None) -> bool:
    """True iff the session is waiting on an external event rather than the user: it has
    an open background task (Monitor / ``run_in_background``) or an armed self-scheduled
    wakeup. Drives the dashboard's ``waiting_external`` ("Waiting…") status overlay."""
    if not jsonl_path:
        return False
    return (bool(scan_open_background_tasks(jsonl_path, treat_killed_as_open=False))
            or scan_pending_wakeup(jsonl_path))


def build_rearm_prompt(tasks: list[OpenBgTask]) -> str:
    lines = ["The daemon was restarted (deploy) and these background tasks were "
             "stopped. Relaunch each one with exactly the parameters shown."]
    if any(t.kind != "monitor" for t in tasks):
        lines.append(_GUARD)
    for t in tasks:
        label = t.description or t.name
        lines.append(f"- [{t.kind}] {label}: relaunch the {t.name} tool with input "
                     f"{json.dumps(t.input)}")
    return "\n".join(lines)
