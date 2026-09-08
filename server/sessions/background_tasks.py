"""The engine-neutral vocabulary for a session's *open* background work, the Claude
scanner that derives it from a transcript, and the turn that follows a restart.

Two engines feed the same :class:`OpenBgTask` vocabulary from very different places:

* **Claude** — scanned out of the transcript ``.jsonl`` here. Pure +
  side-effect-free: the transcript is the durable source of truth for both launches
  (assistant ``tool_use``) and terminations (``task-notification``), so no live
  observer or mirror table is needed (constitution Principle VI).
* **Codex** — read live from the app-server's own register of background terminals
  (``server/codex/background.py``). No transcript exists to scan, and none is needed:
  the vendor tracks the processes itself.

They also differ in what a restart can do about it, which is what ``replayable``
records — see that field and :func:`build_rearm_prompt`."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from .transcript_entries import is_tool_result_entry

_TUID = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
_TASK_ID = re.compile(r"<task-id>(.*?)</task-id>")
_STATUS = re.compile(r"<status>(.*?)</status>")
# completed/failed = the task finished on its own. stopped = the CLI's own notice, written
# on resume, that a background agent has no completion record (it died with the previous
# CLI process) — the agent is gone AND the model has already been told, so it is terminal
# for both scans. killed = interrupted (we re-arm it).
_TERMINAL = {"completed", "failed", "stopped"}

# Tools whose background work is a whole model-driven sub-task rather than a process the
# harness runs on the agent's behalf. ``Agent``/``Task`` launch one; ``SendMessage`` to a
# finished agent resumes it under the SendMessage's own tool-use id (the next
# task-notification names that id).
_AGENT_TOOLS = frozenset({"Agent", "Task", "SendMessage"})

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

# The non-replayable half. Deliberately phrased as a notification with no imperative:
# ccswitch is telling the session what it lost, not handing it a script to re-run.
_LOST_HEADER = ("The daemon was restarted (deploy). These background processes you "
                "had left running were stopped with it and were NOT restarted:")
_LOST_GUARD = ("They cannot be restored automatically — the command lines and agent "
               "briefs are yours, not something this restart is willing to re-issue on "
               "your behalf. If "
               "any of them still matters, check the current state first (what the "
               "process had already done, whether its output is still needed) and "
               "decide for yourself whether to start it again. If none does, carry on.")


@dataclass
class OpenBgTask:
    tool_use_id: str
    kind: str          # "monitor" | "task" | "agent" | "terminal"
    name: str          # originating tool name (Monitor / Bash / Agent / SendMessage / BackgroundTerminal)
    input: dict        # verbatim launch input
    description: str
    #: Can a restart put this work back by re-issuing ``input``?
    #:
    #: True for Claude's ``Monitor`` / ``run_in_background`` tools: the input IS a
    #: tool call, the agent owns re-issuing it, and the harness itself decides what
    #: running it means. False for a Codex background terminal, whose ``input`` is a
    #: raw shell command line — re-running one blindly is how a restart duplicates a
    #: side effect (a push, a migration, an install), and Codex publishes no
    #: relaunch primitive to do it safely. False too for a background *agent*
    #: (``kind == "agent"``): its input is a whole brief that commits, pushes and
    #: opens PRs, so re-issuing it is the same duplicated side effect at larger
    #: scale — and the parent still holds the brief if it decides the work is worth
    #: restarting. ``build_rearm_prompt`` therefore *reports* a non-replayable entry
    #: instead of asking for it back.
    #:
    #: Defaults True so a ``pending_rearm`` snapshot written by an older daemon —
    #: which could only ever hold Claude tasks — round-trips unchanged.
    replayable: bool = True

    def to_dict(self) -> dict:
        return {"tool_use_id": self.tool_use_id, "kind": self.kind,
                "name": self.name, "input": self.input, "description": self.description,
                "replayable": self.replayable}

    @classmethod
    def from_dict(cls, d: dict) -> "OpenBgTask":
        return cls(tool_use_id=d.get("tool_use_id", ""), kind=d.get("kind", "task"),
                   name=d.get("name", ""), input=d.get("input") or {},
                   description=d.get("description", ""),
                   replayable=d.get("replayable", True) is not False)


def _is_background_launch(name: str, inp: dict) -> bool:
    """Background by the tool INPUT alone: ``Monitor``, or an explicit
    ``run_in_background``. Sufficient for those two; NOT for an ``Agent``, whose
    background mode is the default and leaves no trace in the input — see
    :func:`_background_ack_task_id`."""
    return name == "Monitor" or bool(inp.get("run_in_background"))


def _background_ack_task_id(entry: dict) -> str | None:
    """The CLI's own verdict on a tool result: is the tool still running in the
    background? Returns the task-id the CLI assigned (``""`` when async but unnamed),
    or ``None`` for a result that IS the tool's finished output.

    The CLI journals every tool result as a ``type:"user"`` entry whose top-level
    ``toolUseResult`` is its structured record of what happened. Observed shapes
    (dobby, 2026-09-07):

    * ``Agent`` in the background → ``{"isAsync": true, "status": "async_launched",
      "agentId": <task-id>, …}``. The same tool in the foreground →
      ``{"status": "completed", …}`` plus the report. The launch INPUT is identical
      in both cases (background is the default, so no ``run_in_background`` key is
      written), which is why the input can never tell them apart.
    * ``Bash`` with ``run_in_background`` → ``{"backgroundTaskId": <task-id>, …}``.
    * ``SendMessage`` to a finished agent → ``{"resumedAgentId": <task-id>, …}``: the
      agent runs again, and its next notification names the SendMessage's tool-use id.
    """
    tur = entry.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    if tur.get("isAsync") or tur.get("status") == "async_launched":
        return str(tur.get("agentId") or "")
    for key in ("backgroundTaskId", "resumedAgentId"):
        if tur.get(key):
            return str(tur[key])
    return None


def _tool_result_ids(entry: dict) -> list[str]:
    """tool_use ids this ``type:"user"`` entry carries results for."""
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [str(b["tool_use_id"]) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")]


def _launch(tid: str, name: str, inp: dict) -> OpenBgTask:
    if name == "Monitor":
        kind = "monitor"
    elif name in _AGENT_TOOLS:
        kind = "agent"
    else:
        kind = "task"
    # SendMessage carries its one-line label as ``summary``, not ``description``.
    return OpenBgTask(tool_use_id=tid, kind=kind, name=name, input=inp,
                      description=str(inp.get("description") or inp.get("summary") or ""),
                      replayable=name not in _AGENT_TOOLS)


def _notification_text(entry: dict) -> str | None:
    """The ``<task-notification>`` text of an entry, from either place the CLI writes
    it: the ``queue-operation`` enqueue (top-level ``content``) or the user-turn
    delivery (``message.content`` as a plain string)."""
    for content in (entry.get("content"), (entry.get("message") or {}).get("content")):
        if isinstance(content, str) and "<task-notification>" in content:
            return content
    return None


def scan_open_background_tasks(
    jsonl_path: str, *, treat_killed_as_open: bool = True
) -> list[OpenBgTask]:
    """Open background tasks (launched, not yet terminated) in a transcript.

    A launch is recognised two ways: from its INPUT (``Monitor``, explicit
    ``run_in_background``) or from the CLI's ACK of its result saying the tool is
    still running (:func:`_background_ack_task_id`) — the only signal there is for a
    background ``Agent``. A launch is terminated by a ``<task-notification>`` naming
    its tool-use id, or naming only the task-id the ack assigned it (the CLI's
    post-restart ``stopped`` notice carries no tool-use id). A notification closes
    every dispatch under its task-id: it means "this agent stopped", whichever
    ``Agent``/``SendMessage`` call last set it going.

    ``treat_killed_as_open`` distinguishes the two callers. Re-arm (the default)
    keeps a ``killed`` task "open" so it can be relaunched. ``is_parked`` passes
    False: a killed task is dead, not a live external wait — and it is re-armed
    under a *new* tool-use-id (nothing ever marks the old killed id
    completed/failed), so counting it as open would pin the session on
    ``waiting_external`` forever.
    """
    terminal = _TERMINAL if treat_killed_as_open else _TERMINAL | {"killed"}
    launches: dict[str, OpenBgTask] = {}
    # tool_use blocks whose result has not been journaled yet (popped on the ack, so
    # this holds only the in-flight calls of the current turn, never the whole file).
    pending: dict[str, tuple[str, dict]] = {}
    by_task: dict[str, set[str]] = {}   # CLI task-id → tool-use ids dispatched under it
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
                        if not tid:
                            continue
                        if _is_background_launch(name, inp):
                            launches[tid] = _launch(tid, name, inp)
                        else:
                            pending[tid] = (name, inp)
            elif o.get("type") == "user":
                task_id = _background_ack_task_id(o)
                for tid in _tool_result_ids(o):
                    call = pending.pop(tid, None)
                    if task_id is None:
                        continue
                    if tid not in launches and call is not None:
                        launches[tid] = _launch(tid, *call)
                    if task_id and tid in launches:
                        by_task.setdefault(task_id, set()).add(tid)
            # Terminations: any entry whose content carries a task-notification with a
            # terminal status (queue-operation enqueue or the user delivery).
            notif = _notification_text(o)
            if notif is not None:
                ms = _STATUS.search(notif)
                if ms and ms.group(1) in terminal:
                    mt, mtask = _TUID.search(notif), _TASK_ID.search(notif)
                    if mt:
                        terminated.add(mt.group(1))
                    if mtask:
                        terminated |= by_task.get(mtask.group(1), set())
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

    Not every ``type:"user"`` entry starts a turn: the CLI journals each tool's
    result as one too (``transcript_entries.is_tool_result_entry``), and the
    ``ScheduleWakeup`` call's own result lands right after the ``tool_use`` — before the
    agent's closing prose. Counting that as a boundary wiped the armed state for every
    real transcript, so a parked session persisted ``done`` ("Your turn") with nothing
    for the user to do. A fired wakeup arrives as a plain prompt (``isMeta`` string
    content, no ``tool_result``), so it still resets.

    Pure given ``now`` (defaults to the current UTC time); side-effect-free otherwise.
    """
    armed_at: datetime | None = None
    delay: float | None = None
    for o in _iter_jsonl(jsonl_path):
        if o.get("type") == "user" and not is_tool_result_entry(o):
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
    """True iff a CLAUDE session is waiting on an external event rather than the user:
    it has an open background task (Monitor / ``run_in_background``) or an armed
    self-scheduled wakeup.

    The Claude half of ``SessionOrchestrator._session_is_parked``, which is what drives
    the dashboard's ``waiting_external`` ("Waiting…") overlay for every engine. Codex
    answers the same question from a live app-server read and has no wakeup half at all
    (``server/codex/background.py``); routing it through here would only ever glob a
    Claude projects tree for a thread id that is not in it."""
    if not jsonl_path:
        return False
    return (bool(scan_open_background_tasks(jsonl_path, treat_killed_as_open=False))
            or scan_pending_wakeup(jsonl_path))


# Tail of the final hand-back message scanned for a solicitation phrase. The closing of
# a message is where an agent puts its ask ("…which do you want?"); scanning the whole
# body would fire on rhetorical questions posed mid-explanation.
_ASK_TAIL_CHARS = 800

# Lowercased substrings that mark an assistant message as handing a decision back to the
# user. Deliberately broad (the cost of a false "Answer needed" is a harmless nudge; the
# cost of a miss is the bug we are fixing). A trailing "?" is handled separately.
_ASK_PHRASES: tuple[str, ...] = (
    "options:", "(recommended)", "recommended)",
    "which option", "which approach", "which one", "which do you", "which would you",
    "would you like", "would you prefer", "do you want", "want me to",
    "should i ", "shall i ", "let me know", "your call", "pick one",
    "how would you like", "two options", "three options", "go with",
)


def text_awaits_user(text: str) -> bool:
    """Heuristic: does this assistant prose hand a question/decision back to the user?

    Public, and asked of prose from EITHER engine — ``_persist_status`` uses it to keep
    the ``waiting_external`` overlay from masking a parked turn that actually needs the
    user ("Answer needed" rather than "Waiting…"), reading the prose itself through
    ``SessionOrchestrator._last_assistant_prose``. It is also the degrade path of
    ``open_question.completion_awaits_user`` when the LLM helper lane is unavailable —
    one phrase list, no parallel copies."""
    t = text.rstrip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    tail = t[-_ASK_TAIL_CHARS:].lower()
    return any(p in tail for p in _ASK_PHRASES)


def last_assistant_text(jsonl_path: str) -> str:
    """Concatenated text of the most recent assistant message that produced any prose.

    Assistant entries that are pure tool_use (no ``text`` block) are skipped: they are
    not a hand-back to the user, so the last *spoken* message is what we judge."""
    last = ""
    for o in _iter_jsonl(jsonl_path):
        if o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if isinstance(content, str):
            if content.strip():
                last = content
            continue
        if not isinstance(content, list):
            continue
        text = "".join(b.get("text") or "" for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            last = text
    return last


def build_rearm_prompt(tasks: list[OpenBgTask]) -> str:
    """The turn delivered after a restart that stopped this session's background work.

    Two sections, because the two halves of the snapshot license different actions.
    Replayable entries are *instructions* — relaunch this tool with these inputs.
    Non-replayable ones are a *report*: named so the agent knows what stopped and can
    judge whether it still matters, with no instruction to re-run anything. That split
    is the whole point (#1284) — a Codex background terminal's ``input`` is a shell
    command line the agent chose, and a restart that re-issues one unasked is a
    restart that duplicates whatever it did.
    """
    replayable = [t for t in tasks if t.replayable]
    reported = [t for t in tasks if not t.replayable]
    lines: list[str] = []
    if replayable:
        lines.append("The daemon was restarted (deploy) and these background tasks "
                     "were stopped. Relaunch each one with exactly the parameters "
                     "shown.")
        if any(t.kind != "monitor" for t in replayable):
            lines.append(_GUARD)
        for t in replayable:
            label = t.description or t.name
            lines.append(f"- [{t.kind}] {label}: relaunch the {t.name} tool with input "
                         f"{json.dumps(t.input)}")
    if reported:
        if lines:
            lines.append("")
        lines.append(_LOST_HEADER)
        for t in reported:
            label = t.description or t.name
            cwd = t.input.get("cwd")
            where = f" (in {cwd})" if cwd else ""
            lines.append(f"- [{t.kind}] {label}{where}")
        lines.append(_LOST_GUARD)
    return "\n".join(lines)
