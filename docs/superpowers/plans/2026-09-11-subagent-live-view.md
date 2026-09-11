# Sub-agent Live View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Task sub-agent drill-down live and interactive: status/progress/usage from the CLI's task notices, a Stop action backed by `stop_task`, and the sub-agent's own permission/question cards answerable inside its thread.

**Architecture:** The translator turns the SDK's `TaskStarted/Progress/Updated/Notification` system messages into three new normalized events tagged with the spawning Task's `parent_tool_use_id`, so the web reducer's existing per-thread routing carries them. The driver keeps a `tool_use_id → task_id` map from those events; it powers both `stop_subagent` (→ SDK `stop_task`) and permission attribution (`ctx.agent_id == task_id`, verified against claude 2.1.268). One new HTTP route reaches the driver through the same orchestrator → fleet → RPC → remote chain `interrupt` uses.

**Tech Stack:** Python 3.12 / FastAPI / `claude_agent_sdk` 0.2.105 · React + TypeScript + vitest · pytest.

**Spec:** `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md`

## Global Constraints

- Branch: `feat/subagent-live-view` (already created off `origin/master`; the spec is committed on it). Never commit to `master`.
- Python: run tests with the deploy venv and a scrubbed env:
  `env -u CCSWITCH_SESSIOND_TLS_KEY -u CCSWITCH_SESSIOND_CLIENT_ALLOWLIST -u CCSWITCH_SESSIOND_TLS_CERT -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_TLS_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest <path> -q`
  Abbreviated below as `PYTEST <path>`.
- Web: `web/node_modules` is a symlink to the deploy checkout (already linked). Run web commands as `(cd web && npm run <script>)` — a persisted `cd web` breaks the Bash hook. The lint gate is `npm run lint` (`--max-warnings 0`).
- Wire keys are camelCase; Python fields snake_case. Every new event's `to_wire()` includes `"type"` and `"ts"`.
- Sub-agent status vocabulary on the wire: `"completed" | "failed" | "stopped"` (the SDK's `killed` is mapped to `stopped`).
- Commit after every task with a message file (`git commit -F /tmp/msg.txt`) ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. The Bash hook denies command text pairing deploy/release/rollout/promote with prod/production — keep those words out of commands.
- Do not add a composer to the sub-agent view (spec §3 non-goal). Do not touch `server/codex/`.
- Lint gate before the PR is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.

---

## File structure

| File | Responsibility |
|---|---|
| `server/sessions/events.py` | `SubAgentStarted`, `SubAgentProgress`, `SubAgentEnded`; `parent_tool_use_id` on `PermissionRequest` / `Question` |
| `server/sessions/translator.py` | task notice → event; `_task_tool` map (task_id → tool_use_id) |
| `server/sessions/sdk_contract.py` | `stop_task` on the client protocol |
| `server/sessions/claude_driver.py` | `_subagent_tasks` map, `stop_subagent`, permission/question attribution |
| `server/sessions/orchestrator.py` | `stop_subagent(session_id, tool_use_id)` |
| `server/sessions/fleet_orchestrator.py`, `server/ipc/orchestrator_rpc.py`, `server/sessions/remote.py` | route the new method like `interrupt` |
| `server/sessions/session_actions.py` | `stop_subagent` action + audit |
| `server/api/sessions.py` | `POST /{session_id}/agents/{tool_use_id}/stop` |
| `server/sessions/subagents.py` | real `parentAgentId` |
| `server/tests/fakes/fake_sdk.py` | task notice builders; `FakeSDKClient.stop_task` |
| `web/src/session/events.ts` | `EvSubAgentStarted/Progress/Ended`; `parentToolUseId` on permission/question events |
| `web/src/session/reducer.ts` | `SubThreadTask`; event cases; card duplication into sub-threads |
| `web/src/session/selectors.ts` | merge thread task state into `SubAgent` |
| `web/src/session/useSessionRuntime.ts` | pass `viewState.subAgents` to the selector |
| `web/src/api/hooks.ts` | `useStopSubAgent` |
| `web/src/components/session/SubSessionView.tsx` | live header, Stop, pinned permission/question panels |
| `web/src/components/session/SubAgentList.tsx`, `SessionMeta.tsx`, `web/src/components/SubAgentRows.tsx` | `stopped` state, activity line |

---

### Task 1: Task notices → `SubAgentStarted` / `SubAgentProgress` / `SubAgentEnded`

**Files:**
- Modify: `server/sessions/events.py` (append after `ToolResult`, before `PermissionRequest`)
- Modify: `server/sessions/translator.py:1-14` (imports), `:31-45` (init), `:58-62` (`_reset`), `:93-114` (SystemMessage branch)
- Modify: `server/tests/fakes/fake_sdk.py:40-92` (builders)
- Test: `server/tests/test_translator_subagent_notices.py`

**Interfaces:**
- Produces:
  ```python
  ev.SubAgentStarted(parent_tool_use_id: str, task_id: str, description: str, agent_type: str, backgrounded: bool = False, depth: int = 1, ts: float | None)
  ev.SubAgentProgress(parent_tool_use_id: str, task_id: str, activity: str, total_tokens: int | None, tool_uses: int | None, duration_ms: int | None, last_tool: str | None, ts)
  ev.SubAgentEnded(parent_tool_use_id: str, task_id: str, status: str, summary: str = "", total_tokens=None, tool_uses=None, duration_ms=None, ts)
  fake_sdk.task_started(task_id, tool_use_id, description="sleep probe", subagent_type="general-purpose", backgrounded=False, depth=1, task_type="local_agent") -> TaskStartedMessage
  fake_sdk.task_progress(task_id, tool_use_id, activity="Running Bash", total_tokens=19591, tool_uses=1, duration_ms=4098, last_tool="Bash") -> TaskProgressMessage
  fake_sdk.task_updated(task_id, status="killed") -> TaskUpdatedMessage
  fake_sdk.task_notification(task_id, tool_use_id, status="completed", summary="sleep probe", usage=None) -> TaskNotificationMessage
  ```

- [ ] **Step 1: Add the fake builders**

Append to `server/tests/fakes/fake_sdk.py` right after `result(...)` (before `drain_turn`). Shapes are copied from the real CLI's frames captured 2026-09-11 (`/tmp/stop_task_spike_fg.log`).

```python
def task_started(task_id: str, tool_use_id: str | None, description: str = "sleep probe",
                 subagent_type: str = "general-purpose", backgrounded: bool = False,
                 depth: int = 1, task_type: str = "local_agent", sid: str = "sess-1",
                 uuid: str = "ts1"):
    """system/task_started as claude 2.1.268 emits it when a Task sub-agent spawns."""
    from claude_agent_sdk import TaskStartedMessage
    data = {"type": "system", "subtype": "task_started", "task_id": task_id,
            "description": description, "subagent_type": subagent_type,
            "is_backgrounded": backgrounded, "spawn_depth": depth, "task_type": task_type,
            "uuid": uuid, "session_id": sid}
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    return TaskStartedMessage(subtype="task_started", data=data, task_id=task_id,
                              description=description, uuid=uuid, session_id=sid,
                              tool_use_id=tool_use_id, task_type=task_type)

def task_progress(task_id: str, tool_use_id: str | None, activity: str = "Running Bash",
                  total_tokens: int = 19591, tool_uses: int = 1, duration_ms: int = 4098,
                  last_tool: str | None = "Bash", sid: str = "sess-1", uuid: str = "tp1"):
    from claude_agent_sdk import TaskProgressMessage
    usage = {"total_tokens": total_tokens, "tool_uses": tool_uses, "duration_ms": duration_ms}
    data = {"type": "system", "subtype": "task_progress", "task_id": task_id,
            "description": activity, "usage": usage, "last_tool_name": last_tool,
            "uuid": uuid, "session_id": sid}
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    return TaskProgressMessage(subtype="task_progress", data=data, task_id=task_id,
                               description=activity, usage=usage, uuid=uuid, session_id=sid,
                               tool_use_id=tool_use_id, last_tool_name=last_tool)

def task_updated(task_id: str, status: str = "killed", sid: str = "sess-1", uuid: str = "tu1"):
    from claude_agent_sdk import TaskUpdatedMessage
    patch = {"status": status, "end_time": 1789150650196}
    data = {"type": "system", "subtype": "task_updated", "task_id": task_id, "patch": patch,
            "uuid": uuid, "session_id": sid}
    return TaskUpdatedMessage(subtype="task_updated", data=data, task_id=task_id, patch=patch,
                              status=status, session_id=sid, uuid=uuid)

def task_notification(task_id: str, tool_use_id: str | None, status: str = "completed",
                      summary: str = "sleep probe", usage: dict | None = None,
                      sid: str = "sess-1", uuid: str = "tn1"):
    from claude_agent_sdk import TaskNotificationMessage
    data = {"type": "system", "subtype": "task_notification", "task_id": task_id,
            "status": status, "output_file": f"/tmp/tasks/{task_id}.output",
            "summary": summary, "uuid": uuid, "session_id": sid}
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    if usage is not None:
        data["usage"] = usage
    return TaskNotificationMessage(subtype="task_notification", data=data, task_id=task_id,
                                   status=status, output_file=data["output_file"],
                                   summary=summary, uuid=uuid, session_id=sid,
                                   tool_use_id=tool_use_id, usage=usage)
```

- [ ] **Step 2: Write the failing tests**

Create `server/tests/test_translator_subagent_notices.py`:

```python
"""Task sub-agent notices (task_started / task_progress / task_updated / task_notification)
become SubAgentStarted / SubAgentProgress / SubAgentEnded, tagged with the spawning Task's
tool-use id. Shapes verified against claude 2.1.268 on 2026-09-11 (spec §2)."""
from server.sessions import events as ev
from server.sessions.translator import Translator
from server.tests.fakes import fake_sdk as f

TID = "toolu_01YNkJcCo55UaJospWhvnJMx"
TASK = "abb42ada82e1382fe"


def _run(msgs):
    tr = Translator(); out = []
    for m in msgs:
        out += tr.feed(m)
    return tr, out


def test_task_started_emits_sub_agent_started():
    _, out = _run([f.task_started(TASK, TID, description="sleep probe",
                                  subagent_type="Explore", backgrounded=True, depth=2)])
    assert len(out) == 1
    e = out[0]
    assert isinstance(e, ev.SubAgentStarted)
    assert (e.parent_tool_use_id, e.task_id, e.description, e.agent_type) == (
        TID, TASK, "sleep probe", "Explore")
    assert e.backgrounded is True and e.depth == 2
    w = e.to_wire()
    assert w["type"] == "subAgentStarted" and w["parentToolUseId"] == TID
    assert w["taskId"] == TASK and w["agentType"] == "Explore" and w["backgrounded"] is True
    assert isinstance(w["ts"], float)


def test_task_started_without_tool_use_id_is_ignored():
    """A Bash/Monitor task has no spawning Task call — nothing to attach it to."""
    _, out = _run([f.task_started(TASK, None, task_type="local_bash")])
    assert out == []


def test_progress_resolves_tool_use_id_from_the_map():
    _, out = _run([f.task_started(TASK, TID), f.task_progress(TASK, None, activity="Reading files",
                                                                total_tokens=7, tool_uses=2,
                                                                duration_ms=99, last_tool="Read")])
    p = out[1]
    assert isinstance(p, ev.SubAgentProgress)
    assert p.parent_tool_use_id == TID and p.activity == "Reading files"
    assert (p.total_tokens, p.tool_uses, p.duration_ms, p.last_tool) == (7, 2, 99, "Read")
    w = p.to_wire()
    assert w["type"] == "subAgentProgress" and w["lastTool"] == "Read" and w["totalTokens"] == 7


def test_progress_for_unknown_task_is_dropped():
    _, out = _run([f.task_progress("nope", None)])
    assert out == []


def test_updated_killed_maps_to_stopped_and_notification_repeats_ended_with_summary():
    """The real kill sequence: task_updated(killed) then task_notification(stopped).
    Both emit Ended (the client merges idempotently); the second carries the summary."""
    _, out = _run([f.task_started(TASK, TID), f.task_updated(TASK, "killed"),
                   f.task_notification(TASK, TID, status="stopped", summary="sleep probe",
                                       usage={"total_tokens": 5, "tool_uses": 1, "duration_ms": 3})])
    ends = [e for e in out if isinstance(e, ev.SubAgentEnded)]
    assert [e.status for e in ends] == ["stopped", "stopped"]
    assert ends[0].parent_tool_use_id == TID and ends[0].summary == ""
    assert ends[1].summary == "sleep probe" and ends[1].total_tokens == 5
    assert ends[1].to_wire()["type"] == "subAgentEnded"


def test_updated_non_terminal_status_emits_nothing():
    _, out = _run([f.task_started(TASK, TID), f.task_updated(TASK, "running")])
    assert len(out) == 1


def test_notification_drops_the_map_entry():
    tr, _ = _run([f.task_started(TASK, TID), f.task_notification(TASK, TID, status="completed")])
    assert TASK not in tr._task_tool
    # A late progress frame for a finished task has nowhere to go.
    assert tr.feed(f.task_progress(TASK, None)) == []


def test_notification_with_tool_use_id_needs_no_prior_start():
    """A daemon that attached mid-run never saw task_started; the notification names
    the Task call itself, so the end still lands on the right thread."""
    _, out = _run([f.task_notification(TASK, TID, status="failed", summary="boom")])
    assert len(out) == 1 and isinstance(out[0], ev.SubAgentEnded)
    assert out[0].parent_tool_use_id == TID and out[0].status == "failed"


def test_map_survives_turn_end_but_not_reset():
    """Background sub-agents outlive the turn that spawned them: their notices arrive
    while the driver is idle. The map is per translator lifetime, cleared by _reset()
    only when the whole translator resets."""
    tr, _ = _run([f.task_started(TASK, TID), f.stream_start(uuid="t1"), f.result(uuid="t1")])
    assert tr._task_tool == {TASK: TID}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTEST server/tests/test_translator_subagent_notices.py -q`
Expected: FAIL — `AttributeError: module 'server.tests.fakes.fake_sdk' has no attribute 'task_started'` (until Step 1 is applied), then `AttributeError: ... has no attribute 'SubAgentStarted'`.

Note on the last test: `_reset()` is called on `ResultMessage`; the test asserts the map survives it. Implement accordingly (do NOT clear `_task_tool` in `_reset`).

- [ ] **Step 4: Add the events**

In `server/sessions/events.py`, insert after the `ToolResult` dataclass (line ~141) and before `PermissionRequest`:

```python
# ── Task sub-agent lifecycle (spec: 2026-09-11-subagent-live-view-design.md §4.1) ──
# All three carry the spawning Task's tool-use id as `parentToolUseId`, the key the web
# reducer already routes sub-agent threads by. Sourced from the CLI's system/task_*
# notices (translator.py), never from the transcript.

@dataclass
class SubAgentStarted:
    parent_tool_use_id: str; task_id: str; description: str; agent_type: str
    backgrounded: bool = False; depth: int = 1
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "subAgentStarted", "parentToolUseId": self.parent_tool_use_id,
                               "taskId": self.task_id, "description": self.description,
                               "agentType": self.agent_type, "backgrounded": self.backgrounded,
                               "depth": self.depth, "ts": self.ts}

@dataclass
class SubAgentProgress:
    parent_tool_use_id: str; task_id: str; activity: str
    total_tokens: int | None = None; tool_uses: int | None = None; duration_ms: int | None = None
    last_tool: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "subAgentProgress", "parentToolUseId": self.parent_tool_use_id,
                               "taskId": self.task_id, "activity": self.activity,
                               "totalTokens": self.total_tokens, "toolUses": self.tool_uses,
                               "durationMs": self.duration_ms, "lastTool": self.last_tool,
                               "ts": self.ts}

@dataclass
class SubAgentEnded:
    parent_tool_use_id: str; task_id: str; status: str   # "completed" | "failed" | "stopped"
    summary: str = ""
    total_tokens: int | None = None; tool_uses: int | None = None; duration_ms: int | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "subAgentEnded", "parentToolUseId": self.parent_tool_use_id,
                               "taskId": self.task_id, "status": self.status, "summary": self.summary,
                               "totalTokens": self.total_tokens, "toolUses": self.tool_uses,
                               "durationMs": self.duration_ms, "ts": self.ts}
```

- [ ] **Step 5: Translate the notices**

In `server/sessions/translator.py`:

Imports (extend the existing `from claude_agent_sdk import (...)`):

```python
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock, TextBlock,
                              RateLimitEvent, TaskStartedMessage, TaskProgressMessage,
                              TaskUpdatedMessage, TaskNotificationMessage,
                              TERMINAL_TASK_STATUSES)
```

Module-level constant after `log = ...`:

```python
# Wire vocabulary for a finished sub-agent (spec §4.1). The SDK's `killed` (a stop_task
# outcome reported by task_updated) is the same end state the notification calls `stopped`.
_ENDED_STATUS = {"completed": "completed", "failed": "failed", "stopped": "stopped",
                 "killed": "stopped"}
```

In `__init__`, after `self._turn_signals`:

```python
        # Task sub-agents: CLI task id -> the spawning Task tool-use id. Lives for the
        # translator's lifetime, NOT the turn: a background sub-agent's notices arrive
        # after the turn that spawned it ended (through the driver's idle listener).
        self._task_tool: dict[str, str] = {}
```

New method (after `_reset`):

```python
    def _sub_agent_notice(self, msg) -> list:
        """system/task_* notices for a Task sub-agent → SubAgent* events (spec §4.2).
        Returns [] for a task with no Task tool-use id to attach to (a Bash/Monitor
        task, or a task this translator never saw start)."""
        if isinstance(msg, TaskStartedMessage):
            tuid = msg.tool_use_id
            if not tuid or (msg.task_type not in (None, "local_agent")):
                return []
            self._task_tool[msg.task_id] = tuid
            d = msg.data or {}
            atype = d.get("subagent_type")
            return [ev.SubAgentStarted(
                tuid, msg.task_id, msg.description or "",
                atype if isinstance(atype, str) and atype else "agent",
                backgrounded=bool(d.get("is_backgrounded", False)),
                depth=int(d.get("spawn_depth") or 1))]
        if isinstance(msg, TaskProgressMessage):
            tuid = msg.tool_use_id or self._task_tool.get(msg.task_id)
            if not tuid:
                return []
            u = msg.usage or {}
            return [ev.SubAgentProgress(
                tuid, msg.task_id, msg.description or "",
                total_tokens=u.get("total_tokens"), tool_uses=u.get("tool_uses"),
                duration_ms=u.get("duration_ms"), last_tool=msg.last_tool_name)]
        if isinstance(msg, TaskUpdatedMessage):
            if msg.status not in TERMINAL_TASK_STATUSES:
                return []
            tuid = self._task_tool.get(msg.task_id)
            if not tuid:
                return []
            return [ev.SubAgentEnded(tuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]
        if isinstance(msg, TaskNotificationMessage):
            tuid = msg.tool_use_id or self._task_tool.pop(msg.task_id, None)
            self._task_tool.pop(msg.task_id, None)
            if not tuid:
                return []
            u = msg.usage or {}
            return [ev.SubAgentEnded(
                tuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"),
                summary=msg.summary or "", total_tokens=u.get("total_tokens"),
                tool_uses=u.get("tool_uses"), duration_ms=u.get("duration_ms"))]
        return []
```

In `feed`, inside `if isinstance(msg, SystemMessage):` add as the FIRST statement of the branch (the task classes subclass `SystemMessage`, and their subtypes never match the `init`/`api_error`/`compact_boundary` checks below):

```python
            if isinstance(msg, (TaskStartedMessage, TaskProgressMessage,
                                TaskUpdatedMessage, TaskNotificationMessage)):
                return self._sub_agent_notice(msg)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTEST server/tests/test_translator_subagent_notices.py server/tests/test_translator.py server/tests/test_events.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/tests/fakes/fake_sdk.py server/tests/test_translator_subagent_notices.py
printf '%s\n' 'feat(sessions): translate task notices into SubAgentStarted/Progress/Ended events' '' 'Refs spec docs/superpowers/specs/2026-09-11-subagent-live-view-design.md §4.1-4.2.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 2: Driver task map + `stop_subagent` + SDK `stop_task` seam

**Files:**
- Modify: `server/sessions/sdk_contract.py:32-63` (`SDKClientProtocol`)
- Modify: `server/tests/fakes/fake_sdk.py:94-178` (`FakeSDKClient`)
- Modify: `server/sessions/claude_driver.py:181-260` (`__init__`), `:1031-1100` (`_handle_msg`), new method next to `interrupt`
- Test: `server/tests/test_driver_subagent_stop.py`

**Interfaces:**
- Consumes: `ev.SubAgentStarted`, `ev.SubAgentEnded` (Task 1); `fake_sdk.task_started/task_notification` (Task 1).
- Produces:
  ```python
  SDKClientProtocol.stop_task(self, task_id: str) -> None
  FakeSDKClient.stopped_tasks: list[str]; async def stop_task(self, task_id: str) -> None
  ClaudeAgentDriver._subagent_tasks: dict[str, str]          # tool_use_id -> task_id
  ClaudeAgentDriver.subagent_task_id(tool_use_id: str) -> str | None
  ClaudeAgentDriver.subagent_tool_use_id(task_id: str) -> str | None
  async ClaudeAgentDriver.stop_subagent(tool_use_id: str) -> str | None   # task id stopped, None if not running
  ```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_driver_subagent_stop.py`:

```python
"""The driver keeps tool_use_id -> task_id for live Task sub-agents and stops one
through the SDK's stop_task control request (spec §4.3). Verified against claude
2.1.268: stop_task kills a FOREGROUND sub-agent at its next tool boundary."""
from __future__ import annotations

import asyncio

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

TID = "toolu_01YNkJcCo55UaJospWhvnJMx"
TASK = "abb42ada82e1382fe"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _driver(clients: list[FakeSDKClient], session_id="s1"):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    return ClaudeAgentDriver(session_id, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=make, registry=PermissionRegistry(), identity="me")


async def _settle(d: ClaudeAgentDriver, pred, timeout=1.0):
    """Poll a driver predicate — the idle listener consumes injected frames on the loop."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("driver never reached the expected state")
        await asyncio.sleep(0.01)


def test_started_registers_and_ended_unregisters_the_task():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(d, lambda: d.subagent_task_id(TID) == TASK)
        assert d.subagent_tool_use_id(TASK) == TID
        clients[0].inject([f.task_notification(TASK, TID, status="completed")])
        await _settle(d, lambda: d.subagent_task_id(TID) is None)
        assert d.subagent_tool_use_id(TASK) is None
        await d.aclose()
    _run(run())


def test_stop_subagent_calls_stop_task_with_the_mapped_id():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(d, lambda: d.subagent_task_id(TID) == TASK)
        assert await d.stop_subagent(TID) == TASK
        assert clients[0].stopped_tasks == [TASK]
        await d.aclose()
    _run(run())


def test_stop_subagent_unknown_returns_none_and_calls_nothing():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)

    async def run():
        await d.start()
        assert await d.stop_subagent("toolu_missing") is None
        assert clients[0].stopped_tasks == []
        await d.aclose()
    _run(run())


def test_stop_subagent_before_start_returns_none():
    d = _driver([])
    assert _run(d.stop_subagent(TID)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTEST server/tests/test_driver_subagent_stop.py -q`
Expected: FAIL with `AttributeError: 'ClaudeAgentDriver' object has no attribute 'subagent_task_id'`.

- [ ] **Step 3: Add `stop_task` to the contract and the fake**

`server/sessions/sdk_contract.py`, inside `SDKClientProtocol` after `interrupt`:

```python
    async def stop_task(self, task_id: str) -> None:
        """Stop ONE running task (a Task sub-agent) by the CLI task id from
        ``task_started``. Control request ``stop_task``; the CLI answers with
        task_updated(killed) + task_notification(stopped) on the message stream."""
        ...
```

`server/tests/fakes/fake_sdk.py`, in `FakeSDKClient.__init__` add `self.stopped_tasks: list[str] = []`, and after `interrupt`:

```python
    async def stop_task(self, task_id: str) -> None:
        """Record the stop. The real CLI answers on the stream (killed/stopped notices);
        a test that needs them injects ``task_updated`` / ``task_notification`` itself."""
        self.stopped_tasks.append(task_id)
```

- [ ] **Step 4: Implement the driver side**

`server/sessions/claude_driver.py`:

In `__init__` next to `self._queue`:

```python
        # Live Task sub-agents: spawning tool-use id -> CLI task id, from the translator's
        # SubAgentStarted/Ended events. Powers stop_subagent (stop_task needs the task id)
        # and permission attribution (a sub-agent's can_use_tool ctx.agent_id IS its task
        # id — verified against claude 2.1.268, spec §2).
        self._subagent_tasks: dict[str, str] = {}
```

In `_handle_msg`, inside the `for e in self._tr.feed(msg):` loop, just before `if isinstance(e, (ev.TextDelta, ev.ToolCall, ev.ToolResult)):`:

```python
            if isinstance(e, ev.SubAgentStarted):
                self._subagent_tasks[e.parent_tool_use_id] = e.task_id
            elif isinstance(e, ev.SubAgentEnded):
                self._subagent_tasks.pop(e.parent_tool_use_id, None)
```

New methods, placed right after `interrupt`:

```python
    def subagent_task_id(self, tool_use_id: str) -> str | None:
        """CLI task id of a live Task sub-agent, or None once it ended / never started."""
        return self._subagent_tasks.get(tool_use_id)

    def subagent_tool_use_id(self, task_id: str) -> str | None:
        """Reverse lookup: the spawning Task tool-use id for a live task id."""
        for tuid, tid in self._subagent_tasks.items():
            if tid == task_id:
                return tuid
        return None

    async def stop_subagent(self, tool_use_id: str) -> str | None:
        """Stop ONE running Task sub-agent via the SDK's stop_task control request.
        Returns the task id it stopped, or None when the sub-agent is not running
        (unknown id, already ended, or no live client). The map entry is NOT dropped
        here: the CLI's own task_notification(stopped) does that, so a stop that the
        CLI rejected leaves the sub-agent visibly running rather than silently gone."""
        task_id = self._subagent_tasks.get(tool_use_id)
        if task_id is None or self._client is None:
            return None
        log.info("driver.stop_subagent session=%s tool_use_id=%s task_id=%s",
                 self.session_id, tool_use_id, task_id)
        await self._client.stop_task(task_id)
        return task_id
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTEST server/tests/test_driver_subagent_stop.py server/tests/test_fake_sdk.py server/tests/test_agent_driver_contract.py -q`
Expected: all PASS. If `test_agent_driver_contract.py` pins the protocol's method set, add `stop_task` there in the same way `interrupt` is listed.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/sdk_contract.py server/sessions/claude_driver.py server/tests/fakes/fake_sdk.py server/tests/test_driver_subagent_stop.py server/tests/test_agent_driver_contract.py
printf '%s\n' 'feat(sessions): driver tracks live sub-agent task ids and stops one via stop_task' '' 'Refs spec §4.3.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 3: Sub-agent permission and question attribution

**Files:**
- Modify: `server/sessions/events.py` (`PermissionRequest` ~line 143, `Question` ~line 170)
- Modify: `server/sessions/claude_driver.py:542` (`_handle_ask_user_question` signature and its `ev.Question(...)` at ~567), `:717-760` (`_can_use_tool`)
- Test: `server/tests/test_driver_subagent_attribution.py`

**Interfaces:**
- Consumes: `ClaudeAgentDriver.subagent_tool_use_id(task_id)` (Task 2).
- Produces: `PermissionRequest.parent_tool_use_id: str | None = None` → wire `parentToolUseId`; `Question.parent_tool_use_id: str | None = None` → wire `parentToolUseId`; `_handle_ask_user_question(tool_input, turn_id, parent_tool_use_id=None)`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_driver_subagent_attribution.py`:

```python
"""A sub-agent's permission / question card carries the spawning Task's tool-use id
(spec §4.3): ToolPermissionContext.agent_id == task_started.task_id (claude 2.1.268)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

TID = "toolu_01YNkJcCo55UaJospWhvnJMx"
TASK = "abb42ada82e1382fe"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _driver(clients, mode="interactive"):
    def make(cwd, config_dir, can_use_tool, m, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode=mode,
                             sdk_factory=make, registry=PermissionRegistry(), identity="me")


async def _settle(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_permission_from_a_live_sub_agent_carries_its_tool_use_id():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        ctx = SimpleNamespace(agent_id=TASK, tool_use_id="toolu_inner")
        task = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/tmp/x"}, ctx))
        await _settle(lambda: any(isinstance(e, ev.PermissionRequest) for e in list(out_q._queue)))
        req = next(e for e in _drain(out_q) if isinstance(e, ev.PermissionRequest))
        assert req.parent_tool_use_id == TID
        assert req.to_wire()["parentToolUseId"] == TID
        await d.resolve_permission(req.perm_id, "deny")
        await task
        await d.aclose()
    _run(run())


def test_permission_with_unknown_agent_id_is_unattributed():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)

    async def run():
        await d.start()
        ctx = SimpleNamespace(agent_id="never-started", tool_use_id="toolu_inner")
        task = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/tmp/x"}, ctx))
        await _settle(lambda: any(isinstance(e, ev.PermissionRequest) for e in list(out_q._queue)))
        req = next(e for e in _drain(out_q) if isinstance(e, ev.PermissionRequest))
        assert req.parent_tool_use_id is None
        assert req.to_wire()["parentToolUseId"] is None
        await d.resolve_permission(req.perm_id, "deny")
        await task
        await d.aclose()
    _run(run())


def test_question_from_a_live_sub_agent_carries_its_tool_use_id():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)
    tool_input = {"questions": [{"header": "Pick", "question": "Which?", "multiSelect": False,
                                 "options": [{"label": "A", "description": ""},
                                             {"label": "B", "description": ""}]}]}

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        ctx = SimpleNamespace(agent_id=TASK, tool_use_id="toolu_q")
        task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input, ctx))
        await _settle(lambda: any(isinstance(e, ev.Question) for e in list(out_q._queue)))
        q = next(e for e in _drain(out_q) if isinstance(e, ev.Question))
        assert q.parent_tool_use_id == TID and q.to_wire()["parentToolUseId"] == TID
        await d.answer_question(q.question_id, ["A"])
        await task
        await d.aclose()
    _run(run())
```

Check the exact names `resolve_permission` / `answer_question` on the driver with `grep -n "async def resolve_permission\|async def answer_question" server/sessions/claude_driver.py` and use whatever the driver exposes (the orchestrator's `resolve_permission` / `answer_question` call them). If the driver resolves through the registry instead, use `d._reg.resolve(...)` the same way `server/tests/test_driver.py:783-840` does for questions.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTEST server/tests/test_driver_subagent_attribution.py -q`
Expected: FAIL — `assert req.parent_tool_use_id == TID` → `AttributeError: 'PermissionRequest' object has no attribute 'parent_tool_use_id'`.

- [ ] **Step 3: Extend the events**

`server/sessions/events.py`, `PermissionRequest`: add the field after `items` and the wire key:

```python
    items: list[str] | None = None
    # The spawning Task's tool-use id when the request came from a sub-agent (its
    # can_use_tool ctx.agent_id, mapped by the driver). None for the main thread.
    parent_tool_use_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "permissionRequest", "permId": self.perm_id, "turnId": self.turn_id,
                               "tool": self.tool, "toolKind": self.kind.value, "input": self.input, "plan": self.plan,
                               "alwaysAllowed": self.always_allowed, "items": self.items,
                               "parentToolUseId": self.parent_tool_use_id, "ts": self.ts}
```

`Question`: add `parent_tool_use_id: str | None = None` after `orphaned` and `"parentToolUseId": self.parent_tool_use_id` to its wire dict.

- [ ] **Step 4: Attribute in the driver**

In `_can_use_tool`:

```python
    async def _can_use_tool(self, tool_name, tool_input, ctx=None):
        turn_id = self._turn()
        # A sub-agent's request names its task id as ctx.agent_id (== task_started.task_id,
        # verified 2026-09-11); map it to the spawning Task call so the card can render
        # inside that sub-agent's thread. Best-effort: unmapped → unattributed, as before.
        agent_id = getattr(ctx, "agent_id", None) if ctx is not None else None
        pid = self.subagent_tool_use_id(agent_id) if agent_id else None
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(tool_input, turn_id, parent_tool_use_id=pid)
```

and in the `self._emit(ev.PermissionRequest(...))` call add `parent_tool_use_id=pid`.

`_handle_ask_user_question(self, tool_input: dict, turn_id: str, parent_tool_use_id: str | None = None)`; pass `parent_tool_use_id=parent_tool_use_id` into every `ev.Question(...)` it constructs.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTEST server/tests/test_driver_subagent_attribution.py server/tests/test_events.py server/tests/test_driver.py server/tests/test_assistant_permissions.py -q`
Expected: all PASS. (`test_events.py` only checks `ts` presence on these events; if any test asserts a whole wire dict for `PermissionRequest`/`Question`, add `"parentToolUseId": None` to its expected dict.)

- [ ] **Step 6: Commit**

```bash
git add server/sessions/events.py server/sessions/claude_driver.py server/tests/test_driver_subagent_attribution.py
printf '%s\n' 'feat(sessions): attribute sub-agent permission and question cards to their Task call' '' 'ctx.agent_id == task_id (claude 2.1.268); the driver maps it to parentToolUseId. Refs spec §4.3.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 4: `stop_subagent` through orchestrator, fleet, RPC, remote, action and HTTP route

**Files:**
- Modify: `server/sessions/orchestrator.py:12632` (after `interrupt`)
- Modify: `server/sessions/fleet_orchestrator.py:109-120` (`_PER_SESSION`)
- Modify: `server/ipc/orchestrator_rpc.py:313-330` (`_SIMPLE`)
- Modify: `server/sessions/remote.py:774` (after `interrupt`)
- Modify: `server/sessions/session_actions.py:45-50` (after `interrupt`)
- Modify: `server/api/sessions.py:1466-1478` (after `interrupt_session`)
- Test: `server/tests/test_session_actions.py`, `server/tests/test_sessions_api_subagent_stop.py`, `server/tests/test_fleet_orchestrator.py` (existing classification test must stay green)

**Interfaces:**
- Consumes: `ClaudeAgentDriver.stop_subagent(tool_use_id) -> str | None` (Task 2).
- Produces:
  ```python
  SessionOrchestrator.stop_subagent(session_id: str, tool_use_id: str) -> str | None
  RemoteOrchestrator.stop_subagent(session_id, tool_use_id)
  session_actions.stop_subagent(orch, db, *, session_id, tool_use_id, identity, via=None) -> {"ok": True, "taskId": str}
  POST /api/sessions/{session_id}/agents/{tool_use_id}/stop  → 200 {"ok": true, "taskId": …} | 404 {"detail": {"code": "SUBAGENT_NOT_RUNNING", ...}}
  audit type "session.subagent_stop", detail {"sessionId", "toolUseId", "taskId"}
  ```

- [ ] **Step 1: Write the failing action test**

Append to `server/tests/test_session_actions.py` (add `self.stop_subagent = AsyncMock(return_value="abb42ada82e1382fe")` to `FakeOrch.__init__`):

```python
# ---------------------------------------------------------------------------
# stop_subagent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_subagent_calls_orchestrator_and_records_audit(orch):
    with _patched_audit() as audit:
        result = await sa.stop_subagent(orch, db=object(), session_id="s1",
                                        tool_use_id="toolu_A", identity="u@example.com")
    orch.stop_subagent.assert_awaited_once_with("s1", "toolu_A")
    args, kwargs = audit.call_args
    assert args[1] == "session.subagent_stop"
    assert args[3] == {"sessionId": "s1", "toolUseId": "toolu_A", "taskId": "abb42ada82e1382fe"}
    assert kwargs["via"] is None
    assert result == {"ok": True, "taskId": "abb42ada82e1382fe"}


@pytest.mark.asyncio
async def test_stop_subagent_not_running_is_404_and_not_audited(orch):
    orch.stop_subagent = AsyncMock(return_value=None)
    with _patched_audit() as audit, pytest.raises(sa.ActionError) as exc:
        await sa.stop_subagent(orch, db=object(), session_id="s1",
                               tool_use_id="toolu_A", identity="u@example.com")
    assert exc.value.code == "SUBAGENT_NOT_RUNNING" and exc.value.http == 404
    audit.assert_not_awaited()
```

- [ ] **Step 2: Write the failing route test**

Create `server/tests/test_sessions_api_subagent_stop.py`:

```python
"""POST /api/sessions/{id}/agents/{tool_use_id}/stop (spec §4.4)."""
import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": ""}


class _FakeSessions:
    def __init__(self, task_id):
        self.stop_subagent = AsyncMock(return_value=task_id)


def _client(task_id):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(task_id)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_stop_route_returns_task_id_and_calls_orchestrator():
    client, fake = _client("abb42ada82e1382fe")
    with patch.dict(os.environ, _ENV), client:
        resp = client.post("/api/sessions/s1/agents/toolu_A/stop", headers=_H)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "taskId": "abb42ada82e1382fe"}
    fake.stop_subagent.assert_awaited_once_with("s1", "toolu_A")


def test_stop_route_404_when_not_running():
    client, _ = _client(None)
    with patch.dict(os.environ, _ENV), client:
        resp = client.post("/api/sessions/s1/agents/toolu_A/stop", headers=_H)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "SUBAGENT_NOT_RUNNING"
```

- [ ] **Step 3: Run both to verify they fail**

Run: `PYTEST server/tests/test_session_actions.py server/tests/test_sessions_api_subagent_stop.py -q`
Expected: FAIL — `AttributeError: module 'server.sessions.session_actions' has no attribute 'stop_subagent'`; route test 404/405 without the `detail.code`.

- [ ] **Step 4: Implement the chain**

`server/sessions/orchestrator.py`, after `interrupt`:

```python
    async def stop_subagent(self, session_id, tool_use_id: str) -> str | None:
        """Stop ONE running Task sub-agent of a live session (spec §4.4). Returns the
        CLI task id stopped, or None when the session has no live driver or the
        sub-agent is not running — the action layer turns None into a 404."""
        d = self._drivers.get(session_id)
        if d is None:
            return None
        stop = getattr(d, "stop_subagent", None)
        if stop is None:           # an engine without Task sub-agents (Codex)
            return None
        return await stop(tool_use_id)
```

`server/sessions/fleet_orchestrator.py`: add `"stop_subagent",` to `_PER_SESSION` right after `"interrupt",`.

`server/ipc/orchestrator_rpc.py`: add `"stop_subagent",` to `_SIMPLE` right after `"interrupt",` (returns `str | None`, JSON-safe).

`server/sessions/remote.py`, after `interrupt`:

```python
    async def stop_subagent(self, session_id, tool_use_id: str):
        return await self._client.call("stop_subagent", session_id=session_id,
                                       tool_use_id=tool_use_id)
```

`server/sessions/session_actions.py`, after `interrupt`:

```python
async def stop_subagent(orch, db, *, session_id: str, tool_use_id: str, identity: str,
                        via: str | None = None) -> dict:
    """Stop one running Task sub-agent (spec 2026-09-11 §4.4). 404 when it is not
    running: the button only renders while the thread reports running, so this is a
    race with the sub-agent finishing, not a path a user can take on purpose."""
    task_id = await orch.stop_subagent(session_id, tool_use_id)
    if task_id is None:
        raise ActionError(
            "SUBAGENT_NOT_RUNNING",
            "That sub-agent is not running — it may have already finished.",
            404,
        )
    await _audit(db, "session.subagent_stop", identity,
                 {"sessionId": session_id, "toolUseId": tool_use_id, "taskId": task_id}, via)
    return {"ok": True, "taskId": task_id}
```

`server/api/sessions.py`, after `interrupt_session`:

```python
@router.post("/{session_id}/agents/{tool_use_id}/stop")
async def stop_session_subagent(
    session_id: str,
    tool_use_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Stop one running Task sub-agent (the SDK ``stop_task`` control request).
    404 ``SUBAGENT_NOT_RUNNING`` when the sub-agent already finished or the session
    is not live."""
    orch = _get_orchestrator(request)
    via = getattr(request.state, "via", None)
    try:
        return await session_actions.stop_subagent(
            orch, db, session_id=session_id, tool_use_id=tool_use_id,
            identity=identity, via=via)
    except ActionError as exc:
        return _action_error_response(exc)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTEST server/tests/test_session_actions.py server/tests/test_sessions_api_subagent_stop.py server/tests/test_fleet_orchestrator.py server/tests/test_pm_rpc_serialization.py -q`
Expected: all PASS (the fleet classification test passes because `stop_subagent` is in `_PER_SESSION`).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/sessions/fleet_orchestrator.py server/ipc/orchestrator_rpc.py server/sessions/remote.py server/sessions/session_actions.py server/api/sessions.py server/tests/test_session_actions.py server/tests/test_sessions_api_subagent_stop.py
printf '%s\n' 'feat(api): POST /sessions/{id}/agents/{toolUseId}/stop stops one Task sub-agent' '' 'Routed like interrupt (orchestrator, fleet _PER_SESSION, RPC _SIMPLE, remote), audited as session.subagent_stop. Refs spec §4.4.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 5: Real `parentAgentId` in the persisted summary

**Files:**
- Modify: `server/sessions/subagents.py:22-51`
- Test: `server/tests/test_subagents.py`

**Interfaces:**
- Produces: `summarize_transcript` entries with `"parentAgentId": <pid of the Task ToolCall>` (None for main-thread Tasks).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_subagents.py`:

```python
def test_nested_task_call_names_its_parent_thread():
    """A Task issued INSIDE a sub-agent's thread carries that thread's key as its
    parent_tool_use_id (replay tags sidecar tool calls with the spawning Task id), so
    the summary nests it instead of flattening every agent to top level."""
    evs = [_task_call("toolu_A"),
           ev.ToolCall("turn-sub", "toolu_B", "Task", ev.tool_kind("Task"),
                       {"subagent_type": "Explore", "description": "deeper"}, "u",
                       parent_tool_use_id="toolu_A")]
    by = {s["agentId"]: s["parentAgentId"] for s in summarize_transcript(evs)}
    assert by == {"toolu_A": None, "toolu_B": "toolu_A"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST server/tests/test_subagents.py -q`
Expected: FAIL — `assert {'toolu_A': None, 'toolu_B': None} == {...}`.

- [ ] **Step 3: Implement**

In `server/sessions/subagents.py`, replace `"parentAgentId": None,` with `"parentAgentId": e.parent_tool_use_id,` and update the module docstring's sentence "Each summary entry's ``parentAgentId`` is None (top-level) for everything replay surfaces" to: "``parentAgentId`` is the thread the Task call was issued in — the ``parent_tool_use_id`` of its ``ToolCall`` event — so a sub-agent spawned by a sub-agent nests under it; None for a main-thread Task." Also update the `summarize_transcript` docstring line "``parentAgentId`` is None (top-level) for everything replay surfaces." accordingly.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTEST server/tests/test_subagents.py server/tests/test_replay_subagent_discovery.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/subagents.py server/tests/test_subagents.py
printf '%s\n' 'fix(sessions): sub-agent summary nests a Task issued inside another sub-agent' '' 'Refs spec §4.5.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 6: Web reducer — sub-agent task state and cards in the sub-thread

**Files:**
- Modify: `web/src/session/events.ts:16-60` (new event interfaces, union, `parentToolUseId` on permission/question)
- Modify: `web/src/session/reducer.ts:68` (`SubThread`), `:373-383` (`removePermissionBlock`), `:400-420` (`resolveQuestionInPlace`), `:854-920` (`permissionRequest`/`question` cases), new cases before `// ── Local synthetic actions`
- Test: `web/src/session/reducer.subagentTask.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export interface EvSubAgentStarted  { type: 'subAgentStarted';  parentToolUseId: string; taskId: string; description: string; agentType: string; backgrounded: boolean; depth: number; ts?: number }
  export interface EvSubAgentProgress { type: 'subAgentProgress'; parentToolUseId: string; taskId: string; activity: string; totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null; lastTool?: string | null; ts?: number }
  export interface EvSubAgentEnded    { type: 'subAgentEnded';    parentToolUseId: string; taskId: string; status: 'completed' | 'failed' | 'stopped'; summary?: string; totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null; ts?: number }
  export type SubThreadTaskStatus = 'running' | 'completed' | 'failed' | 'stopped'
  export interface SubThreadTask { taskId: string; description: string; agentType: string; backgrounded: boolean; depth: number; status: SubThreadTaskStatus; startedAt?: number; endedAt?: number; activity?: string | null; lastTool?: string | null; totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null; summary?: string | null }
  export interface SubThread { turns: Turn[]; openBlockId: string | null; model?: string | null; task?: SubThreadTask }
  ```

- [ ] **Step 1: Write the failing tests**

Create `web/src/session/reducer.subagentTask.test.ts`:

```ts
// @vitest-environment node
/**
 * Sub-agent task lifecycle (subAgentStarted / Progress / Ended) and sub-agent
 * permission/question cards duplicated into the sub-thread (spec 2026-09-11 §5.2).
 */
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'

type Action = Parameters<typeof transcriptReducer>[1]
function reduce(events: Action[], initial: TranscriptState = initialTranscript): TranscriptState {
  return events.reduce((s, ev) => transcriptReducer(s, ev), initial)
}

const TID = 'toolu_A'
const started: SessionEvent = { type: 'subAgentStarted', parentToolUseId: TID, taskId: 'task1', description: 'sleep probe', agentType: 'Explore', backgrounded: false, depth: 1, ts: 1000 }
const progress: SessionEvent = { type: 'subAgentProgress', parentToolUseId: TID, taskId: 'task1', activity: 'Running Bash', totalTokens: 19591, toolUses: 1, durationMs: 4098, lastTool: 'Bash', ts: 2000 }
const ended: SessionEvent = { type: 'subAgentEnded', parentToolUseId: TID, taskId: 'task1', status: 'stopped', summary: '', ts: 3000 }
const endedRich: SessionEvent = { type: 'subAgentEnded', parentToolUseId: TID, taskId: 'task1', status: 'stopped', summary: 'sleep probe', totalTokens: 5, toolUses: 1, durationMs: 3, ts: 3001 }

describe('sub-agent task lifecycle', () => {
  it('started creates the thread with a running task', () => {
    const s = reduce([started])
    const t = s.subAgents[TID]
    expect(t.turns).toEqual([])
    expect(t.task).toMatchObject({ taskId: 'task1', description: 'sleep probe', agentType: 'Explore', status: 'running', startedAt: 1000, depth: 1 })
  })

  it('progress updates activity, usage and last tool', () => {
    const s = reduce([started, progress])
    expect(s.subAgents[TID].task).toMatchObject({ status: 'running', activity: 'Running Bash', totalTokens: 19591, toolUses: 1, durationMs: 4098, lastTool: 'Bash' })
  })

  it('progress before started still creates a running task with unknown identity', () => {
    const s = reduce([progress])
    expect(s.subAgents[TID].task).toMatchObject({ taskId: 'task1', status: 'running', activity: 'Running Bash', agentType: 'agent' })
  })

  it('ended is idempotent: a later ended keeps the terminal status and takes richer data', () => {
    const s1 = reduce([started, progress, ended])
    expect(s1.subAgents[TID].task).toMatchObject({ status: 'stopped', endedAt: 3000, summary: '' })
    const s2 = transcriptReducer(s1, endedRich)
    expect(s2.subAgents[TID].task).toMatchObject({ status: 'stopped', summary: 'sleep probe', totalTokens: 5, endedAt: 3000 })
    // a stray progress after the end never regresses the status
    const s3 = transcriptReducer(s2, progress)
    expect(s3.subAgents[TID].task?.status).toBe('stopped')
  })

  it('replayReset clears task state with the thread and carries it in the stale buffer', () => {
    const live = reduce([{ type: 'turnStart', turnId: 't1' }, { type: 'textDelta', turnId: 't1', text: 'x' }, { type: 'replayComplete' }, started])
    const s = transcriptReducer(live, { type: 'replayReset' })
    expect(s.subAgents).toEqual({})
    expect(s.staleSubAgents[TID].task?.status).toBe('running')
  })
})

describe('sub-agent cards in the sub-thread', () => {
  const perm: SessionEvent = { type: 'permissionRequest', permId: 'p1', turnId: 't1', tool: 'Write', toolKind: 'write', input: {}, plan: false, parentToolUseId: TID }

  it('a permission with parentToolUseId lands in BOTH the main turn and the sub-thread', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, started, perm])
    expect(s.turns[0].blocks.some((b) => b.kind === 'permission' && b.permId === 'p1')).toBe(true)
    const sub = s.subAgents[TID].turns
    expect(sub).toHaveLength(1)
    expect(sub[0].blocks.some((b) => b.kind === 'permission' && b.permId === 'p1')).toBe(true)
  })

  it('a permission without parentToolUseId touches no sub-thread', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, started, { ...perm, parentToolUseId: null }])
    expect(s.subAgents[TID].turns).toEqual([])
  })

  it('permissionResolved and the optimistic _resolvePermission clear both copies', () => {
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, started, perm])
    for (const action of [{ type: 'permissionResolved', permId: 'p1', turnId: 't1', allowed: true } as Action, { type: '_resolvePermission', permId: 'p1' } as Action]) {
      const r = transcriptReducer(s, action)
      expect(r.turns[0].blocks.some((b) => b.kind === 'permission')).toBe(false)
      expect(r.subAgents[TID].turns[0].blocks.some((b) => b.kind === 'permission')).toBe(false)
    }
  })

  it('a question with parentToolUseId is duplicated and resolved in place in both', () => {
    const q: SessionEvent = { type: 'question', questionId: 'q1', turnId: 't1', header: 'Pick', question: 'Which?', options: [], multiSelect: false, parentToolUseId: TID }
    const s = reduce([{ type: 'turnStart', turnId: 't1' }, { type: 'replayComplete' }, started, q])
    const subBlock = s.subAgents[TID].turns[0].blocks.find((b) => b.kind === 'question')
    expect(subBlock).toMatchObject({ questionId: 'q1', live: true })
    const r = transcriptReducer(s, { type: 'questionResolved', questionId: 'q1', turnId: 't1', selected: ['A'] })
    expect(r.turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ answered: ['A'], live: false })
    expect(r.subAgents[TID].turns[0].blocks.find((b) => b.kind === 'question')).toMatchObject({ answered: ['A'], live: false })
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/session/reducer.subagentTask.test.ts)`
Expected: FAIL (type errors on unknown event types are reported by vitest as runtime failures — `subAgents[TID]` undefined).

- [ ] **Step 3: Add the event types**

In `web/src/session/events.ts`:

```ts
// Task sub-agent lifecycle, sourced from the CLI's system/task_* notices (never the
// transcript). `parentToolUseId` is the spawning Task call — the key of the sub-thread.
// Mirrors SubAgentStarted / SubAgentProgress / SubAgentEnded (server/sessions/events.py).
export interface EvSubAgentStarted  { type: 'subAgentStarted';  parentToolUseId: string; taskId: string; description: string; agentType: string; backgrounded: boolean; depth: number; ts?: number }
export interface EvSubAgentProgress { type: 'subAgentProgress'; parentToolUseId: string; taskId: string; activity: string; totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null; lastTool?: string | null; ts?: number }
export interface EvSubAgentEnded    { type: 'subAgentEnded';    parentToolUseId: string; taskId: string; status: 'completed' | 'failed' | 'stopped'; summary?: string; totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null; ts?: number }
```

Add `parentToolUseId?: string | null` to `EvPermissionRequest` and `EvQuestion` (after `items` / after `orphaned`), and add the three new interfaces to the `SessionEvent` union.

- [ ] **Step 4: Reducer state and cases**

In `web/src/session/reducer.ts`:

Replace the `SubThread` line:

```ts
export type SubThreadTaskStatus = 'running' | 'completed' | 'failed' | 'stopped'
/** Live task state of a Task sub-agent, from subAgentStarted/Progress/Ended. Absent on
 *  a replayed session (the CLI journals no task notices), where the Task tool result
 *  is the only status source — see selectSubAgents. */
export interface SubThreadTask {
  taskId: string; description: string; agentType: string; backgrounded: boolean; depth: number
  status: SubThreadTaskStatus
  startedAt?: number; endedAt?: number
  activity?: string | null; lastTool?: string | null
  totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null
  summary?: string | null
}
export interface SubThread { turns: Turn[]; openBlockId: string | null; model?: string | null; task?: SubThreadTask }
```

Helper next to `removePermissionBlock`:

```ts
const EMPTY_THREAD: SubThread = { turns: [], openBlockId: null }

/** The sub-thread for `pid`, created empty when absent. */
function threadOf(state: TranscriptState, pid: string): SubThread {
  return state.subAgents[pid] ?? EMPTY_THREAD
}

/**
 * Append `block` to the LAST assistant turn of a sub-thread (creating one — id
 * `card:<block.id>` — when the thread has no assistant turn yet). Used to mirror a
 * sub-agent's permission/question card into its own thread while the main transcript
 * keeps its copy; both are keyed by permId/questionId so resolution clears both.
 */
function appendCardToThread(thread: SubThread, block: Block): SubThread {
  const idx = [...thread.turns].reverse().findIndex((t) => t.role === 'assistant')
  if (idx === -1) {
    return { ...thread, turns: [...thread.turns, { id: `card:${block.id}`, role: 'assistant', blocks: [block], fabricated: true }] }
  }
  const at = thread.turns.length - 1 - idx
  const turn = thread.turns[at]
  const dupe = turn.blocks.some((b) => b.id === block.id)
  if (dupe) return thread
  return { ...thread, turns: replaceTurn(thread.turns, at, (t) => ({ ...t, blocks: [...t.blocks, block] })) }
}

/** Map every sub-thread's turns through `fn`; returns the same object when nothing changed. */
function mapSubThreadTurns(subAgents: Record<string, SubThread>, fn: (turns: Turn[]) => Turn[]): Record<string, SubThread> {
  let changed = false
  const next: Record<string, SubThread> = {}
  for (const [pid, thread] of Object.entries(subAgents)) {
    const turns = fn(thread.turns)
    if (turns !== thread.turns) changed = true
    next[pid] = turns === thread.turns ? thread : { ...thread, turns }
  }
  return changed ? next : subAgents
}
```

Rewrite `removePermissionBlock` so it also walks sub-threads:

```ts
function removePermissionBlock(state: TranscriptState, permId: string): TranscriptState {
  const strip = (turns: Turn[]): Turn[] => {
    let changed = false
    const next = turns.map((turn) => {
      const hasIt = turn.blocks.some((b) => b.kind === 'permission' && b.permId === permId)
      if (!hasIt) return turn
      changed = true
      return { ...turn, blocks: turn.blocks.filter((b) => !(b.kind === 'permission' && b.permId === permId)) }
    })
    return changed ? next : turns
  }
  const turns = strip(state.turns)
  const subAgents = mapSubThreadTurns(state.subAgents, strip)
  if (turns === state.turns && subAgents === state.subAgents) return state
  return { ...state, turns, subAgents }
}
```

Rewrite `resolveQuestionInPlace` the same way (keep its docstring):

```ts
function resolveQuestionInPlace(state: TranscriptState, questionId: string, answered: string[]): TranscriptState {
  const resolve = (turns: Turn[]): Turn[] => {
    let changed = false
    const next = turns.map((turn) => {
      const hasIt = turn.blocks.some((b) => b.kind === 'question' && b.questionId === questionId)
      if (!hasIt) return turn
      changed = true
      return {
        ...turn,
        blocks: turn.blocks.map((b) =>
          b.kind === 'question' && b.questionId === questionId ? { ...b, live: false, answered } : b),
      }
    })
    return changed ? next : turns
  }
  const turns = resolve(state.turns)
  const subAgents = mapSubThreadTurns(state.subAgents, resolve)
  if (turns === state.turns && subAgents === state.subAgents) return state
  return { ...state, turns, subAgents }
}
```

In the `permissionRequest` case, after `const newBlock: Block = {...}` and the main-thread `nextTurns`, mirror into the sub-thread:

```ts
      const pid = ev.parentToolUseId
      const subAgents = pid
        ? { ...state.subAgents, [pid]: appendCardToThread(threadOf(state, pid), newBlock) }
        : state.subAgents
      return { ...state, turns: nextTurns, subAgents }
```

In the `question` case, same treatment for the `newBlock` branch (the "existing" early-return branches stay as they are):

```ts
      const pid = ev.parentToolUseId
      const subAgents = pid
        ? { ...state.subAgents, [pid]: appendCardToThread(threadOf(state, pid), newBlock) }
        : state.subAgents
      return { ...state, turns: nextTurns, subAgents }
```

New cases, inserted just before `// ── Local synthetic actions`:

```ts
    // ── Task sub-agent lifecycle ──────────────────────────────────────────────
    // Sourced from the CLI's task notices (spec 2026-09-11 §5.2). Each creates the
    // thread when needed; `subAgentEnded` is idempotent and never regresses a
    // terminal status — the kill sequence delivers it twice (killed, then stopped
    // with the summary), and a stray progress frame can trail the end.
    case 'subAgentStarted': {
      const cur = threadOf(state, ev.parentToolUseId)
      const task: SubThreadTask = {
        ...cur.task,
        taskId: ev.taskId, description: ev.description, agentType: ev.agentType,
        backgrounded: ev.backgrounded, depth: ev.depth,
        status: cur.task?.status ?? 'running',
        startedAt: cur.task?.startedAt ?? ev.ts,
      }
      return { ...state, subAgents: { ...state.subAgents, [ev.parentToolUseId]: { ...cur, task } } }
    }
    case 'subAgentProgress': {
      const cur = threadOf(state, ev.parentToolUseId)
      const base: SubThreadTask = cur.task ?? {
        taskId: ev.taskId, description: '', agentType: 'agent', backgrounded: false, depth: 1,
        status: 'running', startedAt: ev.ts,
      }
      const task: SubThreadTask = {
        ...base, activity: ev.activity, lastTool: ev.lastTool ?? base.lastTool ?? null,
        totalTokens: ev.totalTokens ?? base.totalTokens ?? null,
        toolUses: ev.toolUses ?? base.toolUses ?? null,
        durationMs: ev.durationMs ?? base.durationMs ?? null,
      }
      return { ...state, subAgents: { ...state.subAgents, [ev.parentToolUseId]: { ...cur, task } } }
    }
    case 'subAgentEnded': {
      const cur = threadOf(state, ev.parentToolUseId)
      const base: SubThreadTask = cur.task ?? {
        taskId: ev.taskId, description: '', agentType: 'agent', backgrounded: false, depth: 1,
        status: 'running',
      }
      const task: SubThreadTask = {
        ...base, status: ev.status, endedAt: base.endedAt ?? ev.ts,
        summary: ev.summary || base.summary || null,
        totalTokens: ev.totalTokens ?? base.totalTokens ?? null,
        toolUses: ev.toolUses ?? base.toolUses ?? null,
        durationMs: ev.durationMs ?? base.durationMs ?? null,
      }
      return { ...state, subAgents: { ...state.subAgents, [ev.parentToolUseId]: { ...cur, task } } }
    }
```

`subAgentProgress` must not regress a terminal status: `base.status` is kept as-is (the spread keeps `status`), which satisfies the test because `status` is not overwritten. `replayReset` already sets `subAgents: {}` and captures `staleSubAgents`, so no change there.

- [ ] **Step 5: Run to verify it passes**

Run: `(cd web && npx vitest run src/session/)`
Expected: all reducer/selectors tests PASS including the new file.

- [ ] **Step 6: Typecheck and commit**

Run: `(cd web && npm run typecheck)` — expected clean.

```bash
git add web/src/session/events.ts web/src/session/reducer.ts web/src/session/reducer.subagentTask.test.ts
printf '%s\n' 'feat(web): reducer tracks sub-agent task state and mirrors its cards into the sub-thread' '' 'Refs spec §5.1-5.2.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 7: Selector merge + `stopped` state in the rows and strip

**Files:**
- Modify: `web/src/session/selectors.ts:34-71`
- Modify: `web/src/session/useSessionRuntime.ts:513`
- Modify: `web/src/components/session/SubAgentList.tsx:6-16, 51-53`
- Modify: `web/src/components/session/SessionMeta.tsx:89-99`
- Modify: `web/src/components/SubAgentRows.tsx:7-11`
- Modify: `web/src/api/types.ts:297-303` (`SubAgentSummary.status` gains `'stopped'` — server maps stopped→failed today, but the type must admit it for the tree)
- Test: `web/src/session/__tests__/selectors.test.ts`, `web/src/components/session/SubAgentList.test.tsx` (existing; add a case)

**Interfaces:**
- Consumes: `SubThread.task` (Task 6).
- Produces:
  ```ts
  export type SubAgentStatus = 'running' | 'done' | 'failed' | 'stopped'
  export interface SubAgent { toolId: string; type: string; description: string; status: SubAgentStatus; startedAt?: number; endedAt?: number;
                              taskId?: string; activity?: string | null; lastTool?: string | null; totalTokens?: number | null; toolUses?: number | null; summary?: string | null; backgrounded?: boolean }
  export function selectSubAgents(turns: Turn[], threads?: Record<string, SubThread>): SubAgent[]
  ```

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/__tests__/selectors.test.ts` (add `import type { SubThread } from '../reducer'`):

```ts
describe('selectSubAgents merges live task state', () => {
  const threads: Record<string, SubThread> = {
    x1: { turns: [], openBlockId: null, task: { taskId: 'task1', description: 'scan', agentType: 'Explore', backgrounded: true, depth: 1, status: 'stopped', startedAt: 1000, endedAt: 3000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 1, summary: 'halted' } },
  }
  it('task status wins over the tool-result derivation', () => {
    const [a] = selectSubAgents([toolTurn('Task', false)], threads)
    expect(a.status).toBe('stopped')
    expect(a).toMatchObject({ taskId: 'task1', activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 1, summary: 'halted', backgrounded: true, endedAt: 3000 })
  })
  it('completed maps to done; a running task stays running even with no result', () => {
    const done = { x1: { ...threads.x1, task: { ...threads.x1.task!, status: 'completed' as const } } }
    expect(selectSubAgents([toolTurn('Task', false)], done)[0].status).toBe('done')
    const running = { x1: { ...threads.x1, task: { ...threads.x1.task!, status: 'running' as const, endedAt: undefined } } }
    expect(selectSubAgents([toolTurn('Task', true)], running)[0].status).toBe('running')
  })
  it('falls back to the tool result when the thread has no task (replayed session)', () => {
    expect(selectSubAgents([toolTurn('Task', true)], { x1: { turns: [], openBlockId: null } })[0].status).toBe('done')
    expect(selectSubAgents([toolTurn('Task', false)])[0].status).toBe('running')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/session/__tests__/selectors.test.ts)`
Expected: FAIL — `expected 'running' to be 'stopped'`.

- [ ] **Step 3: Implement the selector**

In `web/src/session/selectors.ts` replace the `SubAgentStatus`/`SubAgent`/`selectSubAgents` block:

```ts
export type SubAgentStatus = 'running' | 'done' | 'failed' | 'stopped'

export interface SubAgent {
  toolId: string       // the spawning Task tool-use id (= key into state.subAgents)
  type: string         // subagent_type, e.g. "Explore" (falls back to "agent")
  description: string
  status: SubAgentStatus
  startedAt?: number   // ms epoch the Task tool call was issued (the block's create-time ts)
  endedAt?: number     // ms epoch its result arrived; only set once the agent finishes
  // Live task state (subAgentStarted/Progress/Ended); absent on a replayed session.
  taskId?: string
  activity?: string | null
  lastTool?: string | null
  totalTokens?: number | null
  toolUses?: number | null
  summary?: string | null
  backgrounded?: boolean
}

const SUBAGENT_TOOL_NAMES = new Set(['Task', 'Agent'])

const TASK_STATUS: Record<SubThreadTaskStatus, SubAgentStatus> = {
  running: 'running', completed: 'done', failed: 'failed', stopped: 'stopped',
}

/**
 * Extract the list of sub-agents from the transcript.
 *
 * Sub-agents surface to the parent session as `Task` or `Agent` tool calls. Status
 * comes from the thread's live task state when the CLI's task notices supplied one
 * (`threads[toolId].task`), else from the paired tool result: present+ok → done,
 * present+!ok → failed, absent → still running. The live state also carries the
 * progress line and usage the sub-agent view and rows show.
 */
export function selectSubAgents(turns: Turn[], threads: Record<string, SubThread> = {}): SubAgent[] {
  const out: SubAgent[] = []
  for (const turn of turns) {
    for (const b of turn.blocks) {
      if (b.kind !== 'tool' || !SUBAGENT_TOOL_NAMES.has(b.name)) continue
      const input = b.input as { subagent_type?: unknown; description?: unknown }
      const type = typeof input.subagent_type === 'string' ? input.subagent_type : 'agent'
      const description = typeof input.description === 'string' ? input.description : ''
      const task = threads[b.toolId]?.task
      const status: SubAgentStatus = task
        ? TASK_STATUS[task.status]
        : b.result == null ? 'running' : b.result.ok ? 'done' : 'failed'
      out.push({
        toolId: b.toolId, type, description, status,
        startedAt: b.ts ?? task?.startedAt,
        endedAt: task?.endedAt ?? b.result?.ts,
        ...(task && {
          taskId: task.taskId, activity: task.activity ?? null, lastTool: task.lastTool ?? null,
          totalTokens: task.totalTokens ?? null, toolUses: task.toolUses ?? null,
          summary: task.summary ?? null, backgrounded: task.backgrounded,
        }),
      })
    }
  }
  return out
}
```

Add `import type { Turn, SubThread, SubThreadTaskStatus } from './reducer'` (merge with the existing `Turn` import).

In `web/src/session/useSessionRuntime.ts:513`: `const subAgents = selectSubAgents(viewState.turns, viewState.subAgents)`.

- [ ] **Step 4: Add `stopped` everywhere a `Record<SubAgentStatus, …>` lives**

`SubAgentList.tsx`: `STATUS_DOT.stopped = 'bg-gray-400'`, `STATUS_LABEL.stopped = 'stopped'`. Replace the description line so a live activity wins:

```tsx
            {(a.activity || a.description) && (
              <span className="text-gray-500 line-clamp-2 dark:text-gray-400">
                {a.status === 'running' && a.activity ? a.activity : a.description}
              </span>
            )}
```

`SessionMeta.tsx`: `DOT_CLASS.stopped = 'bg-gray-400'`; `summaryDot` unchanged (a stopped agent is neither failed nor running, so it falls to `done`, the calm colour).

`SubAgentRows.tsx`: `DOT.stopped = 'bg-gray-400'`.

`web/src/api/types.ts` `SubAgentSummary.status: 'running' | 'done' | 'failed' | 'stopped'`.

Add to `web/src/components/session/SubAgentList.test.tsx` one case rendering `{ toolId: 'x', type: 'Explore', description: 'scan', status: 'running', activity: 'Running Bash' }` and asserting `screen.getByText('Running Bash')`, and one with `status: 'stopped'` asserting `screen.getByText('stopped')`.

- [ ] **Step 5: Run to verify it passes**

Run: `(cd web && npx vitest run src/session/ src/components/session/SubAgentList.test.tsx && npm run typecheck)`
Expected: PASS, typecheck clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/session/selectors.ts web/src/session/useSessionRuntime.ts web/src/session/__tests__/selectors.test.ts web/src/components/session/SubAgentList.tsx web/src/components/session/SubAgentList.test.tsx web/src/components/session/SessionMeta.tsx web/src/components/SubAgentRows.tsx web/src/api/types.ts
printf '%s\n' 'feat(web): sub-agent list derives status, activity and usage from live task state' '' 'Refs spec §5.3.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 8: `SubSessionView` — live header, Stop, answerable cards

**Files:**
- Modify: `web/src/api/hooks.ts:411-418` (add `useStopSubAgent` after `useRenameSession`)
- Modify: `web/src/components/session/SubSessionView.tsx` (whole file)
- Test: `web/src/components/session/__tests__/SubSessionView.test.tsx` (rewrite), `web/src/api/hooks.stopSubAgent.test.tsx`

**Interfaces:**
- Consumes: `rt.subAgents` (Task 7 shape), `rt.state.subAgents[agentId].task`, `rt.onDecision`, `rt.onAnswer`, `rt.onChat`, `rt.armedQuestionId`, `rt.mode`; `POST /api/sessions/{id}/agents/{toolUseId}/stop` (Task 4).
- Produces: `useStopSubAgent(): UseMutationResult<{ ok: boolean; taskId: string }, ApiRequestError, { id: string; toolUseId: string }>`.

- [ ] **Step 1: Write the failing hook test**

Create `web/src/api/hooks.stopSubAgent.test.tsx` (copy the harness from `hooks.cancel.test.tsx`: a `QueryClientProvider` wrapper and a mocked `api.post`). The assertions:

```tsx
it('posts to /sessions/{id}/agents/{toolUseId}/stop', async () => {
  const { result } = renderHook(() => useStopSubAgent(), { wrapper })
  await act(async () => { await result.current.mutateAsync({ id: 's1', toolUseId: 'toolu_A' }) })
  expect(post).toHaveBeenCalledWith('/sessions/s1/agents/toolu_A/stop', {})
})
```

- [ ] **Step 2: Write the failing view test**

Replace `web/src/components/session/__tests__/SubSessionView.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const mutate = vi.fn()
vi.mock('@/api/hooks', () => ({
  useStopSubAgent: () => ({ mutate, isPending: false }),
}))

const onDecision = vi.fn()
const onAnswer = vi.fn()
let thread: Record<string, unknown>
let agents: unknown[]
vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: () => ({
    sessionId: 's1', title: 'Parent Session', mode: 'interactive',
    toolDetailMode: 'compact', thinkingCollapsed: false,
    subAgents: agents, armedQuestionId: null,
    onDecision, onAnswer, onChat: vi.fn(),
    state: {
      turns: [{ id: 'm1', role: 'assistant', blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN_ONLY' }] }],
      subAgents: { toolu_A: thread },
    },
  }),
}))

import { SubSessionView } from '../SubSessionView'

function view() {
  return render(<MemoryRouter><SubSessionView sessionId="s1" agentId="toolu_A" onBack={() => {}} /></MemoryRouter>)
}

beforeEach(() => {
  mutate.mockReset(); onDecision.mockReset(); onAnswer.mockReset()
  thread = {
    turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] }],
    openBlockId: null,
    task: { taskId: 'task1', description: 'map handling', agentType: 'Explore', backgrounded: false, depth: 1, status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3 },
  }
  agents = [{ toolId: 'toolu_A', type: 'Explore', description: 'map handling', status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3, taskId: 'task1' }]
})

describe('SubSessionView', () => {
  it('renders the sub-agent thread, not the main turn, with a breadcrumb', () => {
    view()
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/MAIN_ONLY/)).toBeNull()
    expect(screen.getByText(/Parent Session/)).toBeInTheDocument()
    expect(screen.queryByText(/Read-only/)).toBeNull()
  })

  it('shows the live header: type › description, status, usage line and activity', () => {
    view()
    expect(screen.getByRole('heading', { name: /Explore › map handling/ })).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/19,591 tokens/)).toBeInTheDocument()
    expect(screen.getByText(/3 tools/)).toBeInTheDocument()
    expect(screen.getByText(/last: Bash/)).toBeInTheDocument()
    expect(screen.getByText('Running Bash')).toBeInTheDocument()
  })

  it('Stop calls the mutation with the session and tool-use id', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: /Stop sub-agent/ }))
    expect(mutate).toHaveBeenCalledWith({ id: 's1', toolUseId: 'toolu_A' })
  })

  it('a finished sub-agent shows its status and summary and no Stop button', () => {
    thread = { ...thread, task: { ...(thread.task as object), status: 'stopped', endedAt: Date.now(), summary: 'halted by user' } }
    agents = [{ ...(agents[0] as object), status: 'stopped', summary: 'halted by user' }]
    view()
    expect(screen.getByText('stopped')).toBeInTheDocument()
    expect(screen.getByText('halted by user')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop sub-agent/ })).toBeNull()
  })

  it('a permission card in the sub-thread is answerable in place', () => {
    thread = { ...thread, turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'permission', id: 'p1', permId: 'p1', tool: 'Write', toolKind: 'write', input: { file_path: '/tmp/x' }, plan: false }] }] }
    view()
    fireEvent.click(screen.getByRole('button', { name: /^Allow$/ }))
    expect(onDecision).toHaveBeenCalledWith('p1', 'allow')
  })
})
```

If `PermissionCard`'s allow button has a different accessible name, read `web/src/components/session/PermissionCard.tsx` and use the name it renders.

- [ ] **Step 3: Run to verify they fail**

Run: `(cd web && npx vitest run src/api/hooks.stopSubAgent.test.tsx src/components/session/__tests__/SubSessionView.test.tsx)`
Expected: FAIL — `useStopSubAgent` is not exported; heading not found.

- [ ] **Step 4: Add the hook**

`web/src/api/hooks.ts`, after `useRenameSession`:

```ts
/** Stop ONE running Task sub-agent (POST /sessions/{id}/agents/{toolUseId}/stop). The
 *  CLI answers on the event stream (subAgentEnded), so nothing is invalidated here. */
export function useStopSubAgent() {
  return useMutation<{ ok: boolean; taskId: string }, ApiRequestError, { id: string; toolUseId: string }>({
    mutationFn: ({ id, toolUseId }) =>
      api.post<{ ok: boolean; taskId: string }>(`/sessions/${id}/agents/${encodeURIComponent(toolUseId)}/stop`, {}),
  })
}
```

- [ ] **Step 5: Rewrite the view**

Replace `web/src/components/session/SubSessionView.tsx`:

```tsx
/**
 * SubSessionView — live drill-down into a single Task sub-agent.
 *
 * Parity with Claude Code's own `/tasks` agent view (spec 2026-09-11 §5.5): a header
 * of `type › description`, status, elapsed · tokens · tools · last tool, the CLI's
 * progress line while running (its summary once ended), a Stop action, and the
 * sub-agent's OWN permission/question cards pinned and answerable here. There is no
 * composer: the CLI drops any user input addressed to a sub-agent (spec §1).
 *
 * Data: `rt.subAgents` (selectSubAgents, merged with the thread's live task state) for
 * the header; `rt.state.subAgents[agentId]` for the transcript and the cards.
 */
import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { useStopSubAgent } from '@/api/hooks'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import type { SubAgentStatus } from '@/session/selectors'
import { agentDuration } from './subAgentDuration'
import { TranscriptView } from './TranscriptView'
import { PendingPermissionPanel } from './PendingPermissionPanel'
import { PendingQuestionPanel } from './PendingQuestionPanel'

interface Props {
  sessionId: string
  agentId: string
  onBack: () => void
}

const STATUS_DOT: Record<SubAgentStatus, string> = {
  running: 'bg-blue-500 animate-pulse',
  done: 'bg-emerald-500',
  failed: 'bg-red-500',
  stopped: 'bg-gray-400',
}
const STATUS_LABEL: Record<SubAgentStatus, string> = {
  running: 'running', done: 'completed', failed: 'failed', stopped: 'stopped',
}

export function SubSessionView({ sessionId, agentId, onBack }: Props) {
  const navigate = useNavigate()
  // The breadcrumb returns to the parent INSIDE the list scope it is open in.
  const scope = listScopeForPath(useLocation().pathname)
  const rt = useSessionRuntime(sessionId)
  const thread = rt.state.subAgents[agentId]
  const turns = thread?.turns ?? []
  const meta = rt.subAgents.find((a) => a.toolId === agentId)
  const status: SubAgentStatus = meta?.status ?? 'running'
  const running = status === 'running'
  const now = useNow(1000, running)
  const duration = meta ? agentDuration(meta, now) : null
  const stop = useStopSubAgent()
  const label = meta?.type ?? 'sub-agent'

  const usage = [
    duration,
    meta?.totalTokens != null ? `${meta.totalTokens.toLocaleString()} tokens` : null,
    meta?.toolUses != null ? `${meta.toolUses} ${meta.toolUses === 1 ? 'tool' : 'tools'}` : null,
    running && meta?.lastTool ? `last: ${meta.lastTool}` : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="flex flex-col flex-1 min-h-0 bg-gray-50 dark:bg-gray-900">
      {/* paddingTop pushes content below the iOS status bar; env() is 0 on desktop. */}
      <header
        className={cn('flex flex-col gap-1 px-3 pb-2 border-b',
          'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800')}
        style={{ paddingTop: 'calc(env(safe-area-inset-top) + 0.75rem)' }}
      >
        <div className="flex items-center gap-3">
          <button type="button" onClick={onBack} aria-label="Back to session"
            className={cn('rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
              'border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300',
              'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
            ← Back
          </button>
          <nav aria-label="Breadcrumb" className="text-sm text-gray-600 dark:text-gray-300 truncate">
            <button
              type="button"
              onClick={() => navigate(sessionPath(sessionId, scope))}
              className="opacity-80 hover:underline focus-visible:outline-none focus-visible:underline"
            >
              {rt.title || sessionId.slice(0, 8)}
            </button>
            <span aria-hidden className="mx-1.5 opacity-50">›</span>
            <span className="font-medium">{label}</span>
          </nav>
          {running && (
            <button
              type="button"
              onClick={() => stop.mutate({ id: sessionId, toolUseId: agentId })}
              disabled={stop.isPending}
              aria-label="Stop sub-agent"
              className={cn('ml-auto rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
                'border border-red-300 dark:border-red-700 text-red-700 dark:text-red-300',
                'hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500')}
            >
              Stop
            </button>
          )}
        </div>
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100 truncate">
          {label}{meta?.description ? ` › ${meta.description}` : ''}
        </h2>
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <span aria-hidden className={cn('h-2 w-2 rounded-full', STATUS_DOT[status])} />
          <span className="uppercase tracking-wide">{STATUS_LABEL[status]}</span>
          {usage && <span className="tabular-nums">· {usage}</span>}
        </div>
        {running && meta?.activity && (
          <p className="text-xs italic text-gray-500 dark:text-gray-400 truncate">{meta.activity}</p>
        )}
        {!running && meta?.summary && (
          <p className="text-xs text-gray-600 dark:text-gray-300 line-clamp-3">{meta.summary}</p>
        )}
      </header>
      {/* paddingBottom keeps last item above the home indicator; env() is 0 on desktop. */}
      {/* A `div`, not a `<main>`: the shell's `app.main` region IS the page's single
          main landmark, and a second one nested inside it is a landmark violation. */}
      <div className="flex-1 overflow-hidden flex flex-col" style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}>
        {turns.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {running
                ? 'Sub-agent starting…'
                : 'No live sub-agent activity (open the parent session while it runs to stream it).'}
            </p>
          </div>
        ) : (
          <TranscriptView
            turns={turns}
            toolDetailMode={rt.toolDetailMode}
            thinkingCollapsed={rt.thinkingCollapsed}
            onAnswer={rt.onAnswer}
          />
        )}
        {/* The sub-agent's own asks, pinned like the parent view pins them. Cards are
            mirrored into this thread by the reducer (keyed by permId/questionId), so
            answering here resolves the parent's copy too. */}
        <PendingQuestionPanel
          turns={turns}
          onAnswer={rt.onAnswer}
          onChat={rt.onChat}
          armedQuestionId={rt.armedQuestionId}
        />
        <PendingPermissionPanel
          turns={turns}
          permissionMode={rt.mode}
          onDecision={rt.onDecision}
        />
      </div>
    </div>
  )
}
```

`toLocaleString()` in the test environment renders `19,591` under the default `en-US` locale vitest uses; if the runner's locale differs, assert on `/19.?591 tokens/` instead.

- [ ] **Step 6: Run to verify they pass**

Run: `(cd web && npx vitest run src/api/hooks.stopSubAgent.test.tsx src/components/session/__tests__/SubSessionView.test.tsx && npm run typecheck && npm run lint)`
Expected: PASS, typecheck and lint clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/hooks.ts web/src/api/hooks.stopSubAgent.test.tsx web/src/components/session/SubSessionView.tsx web/src/components/session/__tests__/SubSessionView.test.tsx
printf '%s\n' 'feat(web): sub-agent view goes live — status header, usage, Stop, answerable cards' '' 'Refs spec §5.4-5.5.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 9: Gates, spec touch-up, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md` §4.6 (replay): replace the paragraph with "Out of scope. The CLI journals no task notices into the transcript, so a reopened session shows tool-result-derived status through `selectSubAgents`'s fallback (§5.3); no replay change is needed." (The originally proposed replay `SubAgentEnded` duplicated that fallback.)

- [ ] **Step 1: Server gate (four steps)**

```bash
cd /home/tony/GitHub/.ccswitch/worktrees/viberpit/f9be2add
/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check src/ server/
/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/ --ignore-missing-imports 2>&1 | tail -3
PYTEST tests/ -q 2>&1 | tail -3
```

Then the full server suite in the background, output tee'd, with the test-slot acquire in the SAME shell (see CLAUDE.md § Dev / testing for the acquire loop):

```bash
PYTEST server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/subagent-live-view-server-tests.log | tail -15
```

Expected: ruff clean, mypy `Success`, core tests pass, server suite passes except the known rocky/dobby-only flakes listed in memory (`test_ipc_entrypoint`, reauth timing, two ws timing tests, codex overlay). Re-run any other failure alone before treating it as pre-existing.

- [ ] **Step 2: Web gate**

```bash
(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tail -8 && npm run build 2>&1 | tail -3)
```

Expected: all four green. `npm run test` must be the FULL run — components mocking `@/api/hooks` may need `useStopSubAgent` added to their mock only if they render `SubSessionView` (routes tests); fix any such file by adding `useStopSubAgent: () => ({ mutate: () => {}, isPending: false })` to its mock.

- [ ] **Step 3: Commit the spec touch-up, push, open the PR**

```bash
git add docs/superpowers/specs/2026-09-11-subagent-live-view-design.md
printf '%s\n' 'docs(specs): sub-agent live view — replay needs no change' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
git push -u origin feat/subagent-live-view
```

Check in-flight PRs touching the same files first (CLAUDE.md § "Before implementing"): the GraphQL query with path regex `SubSessionView|subagents|claude_driver|translator|session_actions`. Then write the PR body to `/tmp/pr-body.md`:

```markdown
## Sub-agent live view: status, stop, in-place permissions

Makes the Task sub-agent drill-down live and interactive — parity with Claude Code's own `/tasks` agent view.

**Server**
- Translator turns the CLI's `task_started` / `task_progress` / `task_updated` / `task_notification` notices into `subAgentStarted` / `subAgentProgress` / `subAgentEnded` events tagged with the spawning Task's tool-use id.
- Driver keeps `tool_use_id → task_id`; `stop_subagent` calls the SDK's `stop_task` (verified on claude 2.1.268 to kill a **foreground** sub-agent at its next tool boundary).
- Sub-agent permission/question cards carry `parentToolUseId` (`ctx.agent_id == task_id`, verified).
- `POST /api/sessions/{id}/agents/{toolUseId}/stop`, routed like `interrupt`, audited as `session.subagent_stop`.
- Persisted sub-agent summary nests a Task issued inside another sub-agent.

**Web**
- Reducer tracks per-thread task state and mirrors sub-agent cards into the sub-thread; resolving clears both copies.
- `SubSessionView`: `type › description`, status, elapsed · tokens · tools · last tool, progress line, **Stop**, pinned answerable permission/question panels. "Read-only" badge removed.
- Rows and strip show `stopped` and the live activity line.

**Not included (by design):** a composer in the sub-agent view — the CLI drops user frames addressed to a sub-agent. The interactive path is child-session dispatch, a separate spec.

Spec: `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md`. Plan: `docs/superpowers/plans/2026-09-11-subagent-live-view.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

```bash
gh pr create --title "feat(sessions): sub-agent live view — status, stop, in-place permissions" --body-file /tmp/pr-body.md --base master
gh pr merge <#> --squash --auto
```

Expected: PR open, non-draft, auto-merge armed; report the PR number and the check status.

---

## Self-review

**Spec coverage.** §4.1 events → Task 1. §4.2 translator → Task 1. §4.3 driver map / stop / attribution → Tasks 2–3. §4.4 chain + route + audit → Task 4. §4.5 summary `parentAgentId` → Task 5. §4.6 replay → dropped as redundant (Task 9 amends the spec). §5.1–5.2 types/reducer → Task 6. §5.3 selector → Task 7. §5.4 hook, §5.5 view → Task 8. §6 error handling: 404 path (Task 4), unmapped `agent_id` (Task 3), unknown task dropped (Task 1). §7 tests: each task carries its own; the `_PER_SESSION` classification test is covered by adding the entry (Task 4). §8 one PR → Task 9.

**Placeholders.** None: every step has code, a command and an expected result. The two "verify the exact name" notes (driver resolve/answer method names in Task 3; `PermissionCard` allow-button label in Task 8) point at a specific file to read and give the fallback.

**Type consistency.** `SubAgentStarted(parent_tool_use_id, task_id, description, agent_type, backgrounded, depth)` matches the fake builders and translator (Task 1) and the wire keys the web `EvSubAgentStarted` consumes (Task 6). `stop_subagent` returns `str | None` end to end: driver (Task 2) → orchestrator/remote (Task 4) → action maps `None` → 404. `SubThreadTaskStatus` (`running|completed|failed|stopped`) is what the translator emits (`_ENDED_STATUS`) plus `running`; `TASK_STATUS` in Task 7 maps it onto `SubAgentStatus` (`running|done|failed|stopped`), the vocabulary the rows/strip/view records are keyed by.
