# Restart-Orphaned Interactive Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a daemon restart, a session's open AskUserQuestion is answerable again (the answer is delivered as the next turn's prompt), and an orphaned permission / plan approval says so instead of silently vanishing.

**Architecture:** The system today models "awaiting human" in exactly one way — a live in-process `asyncio.Future` is waiting — and binds UI answerability directly to it. A restart destroys both the future and the SDK subprocess that awaits it, so the request is *unresolvable by construction*; it can only be **re-asked**, with the user's choice delivered as a new prompt. We add that second model: a small `orphaned_interaction()` detector over the flags `rehydrate` already captures, an `orphaned` flag on the `Question` wire event, and an answer path that falls back to `ensure_driver` + `send_prompt` when no live future exists. No new DB columns — `interrupted_by_restart` + `interrupted_from_status` already carry the signal and already self-clear on the next turn.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (server), React + TypeScript + Vitest (web), pytest.

**Spec:** This document's "Problem & Evidence" section below. Companion prior art: `docs/audits/2026-09-02-restart-stranded-state-audit.md` (same root shape — process-local state lost across a restart), issues #434 (interrupted-by-restart flag), #864 (deploy snapshot/restore), #938 (resume interrupted sessions).

---

## Problem & Evidence

**Symptom:** after a restart a session renders its question block with no way to choose an answer, leaving it in an unknown state.

**Causal chain (5-whys):**

1. Inline question cards render unconditionally `readOnly` (`web/src/components/session/Turn.tsx:86`); the answerable copy lives only in `PendingQuestionPanel`, which selects blocks where `b.live === true`.
2. Replay creates question blocks with `live: !state.replaying` → `false` (`web/src/session/reducer.ts:712`). They are upgraded to `live: true` only by the server's post-`replayComplete` re-emit (`server/bridge/ws.py:545`).
3. That re-emit iterates `driver.pending_questions()` → `QuestionRegistry.pending()`, whose `_pending` dict of `asyncio.Future`s died with the process (`server/sessions/questions.py:17`).
4. It cannot simply be restored: the waiter is the SDK subprocess's `can_use_tool` callback, which died too. A pending question is a **live rendezvous between two processes**, not data.
5. **Root cause:** there is no model for an interactive request whose waiter is gone but whose question is still legitimately open. The transcript faithfully replays the question (a real `tool_use`), while every affordance for acting on it is gated on process-local state a restart guarantees is absent.

**Live evidence** — session `b37aba5d-6d3d-4501-a74d-e3561234f618`, transcript `~/.claude/projects/-home-tony-GitHub--ccswitch-worktrees-KinDash-b37aba5d/fa1cd767-c8a6-43ed-a6d7-6b02ec7e3c67.jsonl`:

- line 127, `2026-09-03T07:03:41Z` — assistant emits `AskUserQuestion` `toolu_01BRXfxzxajebTr8a3tXQpxx`, header `"Disposition"`.
- ~3.5h gap; 4 `ccswitch-daemon.service` lifecycle events in the window. Question on screen, unanswerable.
- line 128, `2026-09-03T10:29:37.850Z` — `tool_result` `is_error=True`: *"The user doesn't want to proceed with this tool use. The tool use was rejected… STOP what you are doing and wait for the user to tell you how to proceed."* Then line 129 `[Request interrupted by user for tool use]`.
- `systemctl show ccswitch-daemon.service -p ActiveEnterTimestamp` → `2026-09-03 06:29:39 EDT` = `10:29:39Z`, **two seconds after** that rejection.

So the user's decision was never delivered, and the assistant was told the user **rejected** it — a false statement about the user's intent, indistinguishable in the transcript from a genuine dismissal. This is the "unknown state."

**The three related failure modes** (all normalize to `idle` at rehydrate; #938 explicitly excluded all three from resume — `server/sessions/types.py:22-25` *"awaiting-human … must NOT be nudged"*):

| Pre-restart status | Transcript-replayable | Today |
|---|---|---|
| `waiting_question` | yes (`AskUserQuestion` tool_use) | dead read-only card |
| `waiting_input` (permission) | no (`can_use_tool` leaves no record) | card vanishes; turn silently dead |
| `ready_review` (plan) | yes (`ExitPlanMode` tool_use) | renders as a raw tool card, no approval affordance |

**Also fixed for free:** autopilot's stuck-session handler (`server/sessions/orchestrator.py:4829-4835`) already calls `ensure_driver` then `answer_question` — which returns `False` for every orphaned question, so autopilot silently fails to unstick exactly these sessions.

## Global Constraints

- Python: no new DB columns. `SessionRecord.interrupted_by_restart` + `interrupted_from_status` are the signal; both are already set by `rehydrate()` and cleared together by `_persist_status` on any non-`idle` status.
- Never resurrect or fabricate an `asyncio.Future`. An orphaned request is answered by sending a **new prompt**, never by resolving a dead future.
- The live (non-orphaned) answer path must be byte-for-byte unchanged in behaviour — orphan handling is strictly a fallback after the live path declines.
- Follow the repo's existing "single source of truth" rule: the orphan detector is ONE function used by every caller; the transcript question reconstruction reuses `server/sessions/replay.py`, never a second parser.
- Frontend: `web/src/session/reducer.ts` `Block` union and `web/src/api/types.ts` are the status/shape vocabularies — extend them, don't shadow them.
- Test commands (run from repo root, worktree has a `.venv` symlink to the main checkout):
  - `./.venv/bin/python -m pytest server/tests/<file> -q -p no:randomly`
  - `npm --prefix web exec -- vitest run <path>`
  - Full gate before finishing: `./.venv/bin/python -m ruff check src/ server/`, `./.venv/bin/python -m mypy server/ --ignore-missing-imports`, `./.venv/bin/python -m pytest tests/ -q`, `./.venv/bin/python -m pytest server/tests/ -q`, `npm --prefix web run lint`, `npm --prefix web run test`, `npm --prefix web exec -- tsc -b web/tsconfig.json --force`.
  - NOTE: `npm run typecheck` is vacuous here (root tsconfig is `"files": []` + project references). Use `tsc -b` as above.
- Never `cd` into `web/`; always `npm --prefix web`.

## File Structure

| File | Responsibility |
|---|---|
| `server/sessions/orphaned_asks.py` (**create**) | The one definition of "this session's human-facing ask died with the daemon": the status set, the detector over a `SessionRecord`, and the prompt text that delivers an orphaned answer. Dependency-free and table-testable, mirroring `server/sessions/status.py`. |
| `server/sessions/events.py` (modify) | `Question` gains `orphaned: bool = False` → `"orphaned"` on the wire. |
| `server/sessions/replay.py` (modify) | Add `last_question_events(path)` reusing the existing `AskUserQuestion` reconstruction so the attach path never parses the transcript a second way. |
| `server/sessions/orchestrator.py` (modify) | `orphaned_interaction()` accessor over `_records`; `answer_question` becomes async with an orphan fallback; `_deliver_orphaned_answer`. |
| `server/bridge/ws.py` (modify) | After the live pending-question re-emit, re-emit the last transcript question with `orphaned=True` when the session has an orphaned question. |
| `web/src/session/reducer.ts` (modify) | `question` block gains `orphaned?: boolean`; the live-upgrade branch stamps it. |
| `web/src/components/session/QuestionCard.tsx` (modify) | Banner explaining an orphaned question, so the user knows answering starts a new turn. |
| `web/src/components/session/PendingQuestionPanel.tsx` (modify) | Region label reflects the orphaned case. |
| `web/src/components/session/InterruptedAskNotice.tsx` (**create**) | The permission/plan orphan notice + "Ask again" action. |
| `web/src/components/session/SessionBody.tsx` (modify) | Render `InterruptedAskNotice` above the composer. |
| `web/src/api/types.ts` (modify) | `Session.interruptedFromStatus` (already on the wire from `types.py:185`, undeclared on the client). |

---

### Task 1: The orphan detector (pure module)

**Files:**
- Create: `server/sessions/orphaned_asks.py`
- Test: `server/tests/test_orphaned_asks.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ORPHANABLE_ASK_STATUSES: frozenset[str]` — `{"waiting_question", "waiting_input", "ready_review"}`
  - `orphaned_ask_kind(interrupted_by_restart: bool, interrupted_from_status: str | None) -> str | None` — returns `"question"`, `"permission"`, `"plan"`, or `None`.
  - `orphaned_answer_prompt(selected: list[str]) -> str`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orphaned_asks.py`:

```python
"""An interactive request whose waiter died with the daemon is 'orphaned'.

It can never be RESOLVED (the asyncio.Future and the SDK subprocess that awaited
it are both gone) — only RE-ASKED, with the user's choice delivered as the next
turn's prompt. This module is the one definition of that condition.
"""
import pytest

from server.sessions.orphaned_asks import (
    ORPHANABLE_ASK_STATUSES,
    orphaned_ask_kind,
    orphaned_answer_prompt,
)


@pytest.mark.parametrize("status,kind", [
    ("waiting_question", "question"),
    ("waiting_input", "permission"),
    ("ready_review", "plan"),
])
def test_awaiting_human_statuses_map_to_their_ask_kind(status, kind):
    assert orphaned_ask_kind(True, status) == kind


@pytest.mark.parametrize("status", ["in_progress", "condensing", "idle", "done", None])
def test_non_awaiting_statuses_are_not_orphaned_asks(status):
    """Dropped WORK is #938's job (resume_interrupted); only dropped ASKS are ours."""
    assert orphaned_ask_kind(True, status) is None


def test_not_orphaned_unless_the_restart_flag_is_set():
    """A session resting at waiting_question with a LIVE driver is not orphaned —
    interrupted_by_restart is what distinguishes 'the daemon died' from 'still waiting'."""
    assert orphaned_ask_kind(False, "waiting_question") is None


def test_every_orphanable_status_has_a_kind():
    """The set and the mapping must not drift apart."""
    for s in ORPHANABLE_ASK_STATUSES:
        assert orphaned_ask_kind(True, s) is not None


def test_answer_prompt_carries_the_selection_and_corrects_the_false_rejection():
    """The CLI closes out the dangling tool_use with 'The user doesn't want to
    proceed… STOP' (observed in prod, transcript line 128). The assistant reads that
    BEFORE our prompt, so the prompt must explicitly disown it or the assistant stops."""
    text = orphaned_answer_prompt(["Close #15205 as duplicate"])
    assert "Close #15205 as duplicate" in text
    assert "restart" in text.lower()
    assert "not" in text.lower() and "reject" in text.lower()


def test_answer_prompt_joins_a_multi_select():
    text = orphaned_answer_prompt(["Alpha", "Beta"])
    assert "Alpha" in text and "Beta" in text


def test_answer_prompt_handles_an_empty_selection():
    """Never emit a prompt that reads as an answer with nothing in it."""
    text = orphaned_answer_prompt([])
    assert text.strip()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest server/tests/test_orphaned_asks.py -q -p no:randomly`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.orphaned_asks'`

- [ ] **Step 3: Write minimal implementation**

Create `server/sessions/orphaned_asks.py`:

```python
"""Interactive requests whose waiter died with the daemon.

A pending AskUserQuestion / permission / plan approval is NOT data: it is a live
rendezvous between the daemon's ``asyncio.Future`` and the SDK subprocess blocked
in its ``can_use_tool`` callback. A restart destroys both, so such a request can
never be RESOLVED afterwards — only RE-ASKED, with the user's choice delivered as
the next turn's prompt.

This module is the single definition of that condition. Kept dependency-free and
side-effect-free so it is exhaustively table-testable, mirroring ``status.py``.
"""
from __future__ import annotations

# Pre-restart statuses that mean a human was being asked something. Deliberately
# the exact complement of RESUME_ELIGIBLE_STATUSES (server/sessions/types.py):
# #938 resumes dropped WORK and explicitly excludes these three; this module owns
# them. If one set grows, the other must be reconsidered.
ORPHANABLE_ASK_STATUSES: frozenset[str] = frozenset({
    "waiting_question", "waiting_input", "ready_review",
})

_KIND_BY_STATUS: dict[str, str] = {
    "waiting_question": "question",
    "waiting_input": "permission",
    "ready_review": "plan",
}


def orphaned_ask_kind(interrupted_by_restart: bool,
                      interrupted_from_status: str | None) -> str | None:
    """The kind of human-facing ask a restart orphaned, or None.

    Both inputs come from the SessionRecord flags ``rehydrate()`` sets and
    ``_persist_status`` clears together on any non-idle status — so this
    self-clears the moment the session does anything again, with no new column.
    """
    if not interrupted_by_restart:
        return None
    return _KIND_BY_STATUS.get(interrupted_from_status or "")


def orphaned_answer_prompt(selected: list[str]) -> str:
    """The prompt that delivers a user's answer to an orphaned question.

    Two jobs. First, carry the selection. Second — and this is load-bearing —
    disown the tool_result the CLI synthesises for the dangling ``tool_use`` when
    it resumes the transcript: "The user doesn't want to proceed with this tool
    use. The tool use was rejected... STOP what you are doing." The assistant
    reads that immediately before this prompt, so without an explicit correction
    it stops instead of acting on the answer.
    """
    answer = "; ".join(s for s in selected if s) or "(no option selected)"
    return (
        "[Answering the question that a daemon restart interrupted]\n\n"
        f"My answer: {answer}\n\n"
        "Note: that question's tool call was closed out automatically when the "
        "daemon restarted, and the transcript may show it as rejected or "
        "interrupted. I did not reject it — the restart did. Please treat the "
        "answer above as my response and continue."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest server/tests/test_orphaned_asks.py -q -p no:randomly`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orphaned_asks.py server/tests/test_orphaned_asks.py
git commit -m "feat(sessions): define restart-orphaned interactive asks"
```

---

### Task 2: `orphaned` on the Question wire event + transcript reconstruction

**Files:**
- Modify: `server/sessions/events.py:113-119` (the `Question` dataclass)
- Modify: `server/sessions/replay.py` (add `last_question_events`)
- Test: `server/tests/test_orphaned_question_replay.py`

**Interfaces:**
- Consumes: `server.sessions.replay._assistant_block_events` (existing, internal).
- Produces:
  - `ev.Question(..., orphaned: bool = False)` → wire key `"orphaned"`.
  - `replay.last_question_events(path: str) -> list[ev.Question]` — the Question events reconstructed from the LAST `AskUserQuestion` tool_use in the transcript, with the same `f"{tool_use_id}-{index}"` ids replay already assigns. `[]` when the file is missing/unreadable or has no such tool_use.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orphaned_question_replay.py`:

```python
"""Re-offering an orphaned question needs the SAME question ids replay assigned,
or the client's reducer creates a second, duplicate card instead of upgrading the
one already on screen."""
import json

from server.sessions import events as ev
from server.sessions.replay import last_question_events


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _ask(tool_id, header, options):
    return {"type": "assistant", "uuid": f"u-{tool_id}", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {"questions": [
            {"header": header, "question": f"{header}?", "multiSelect": False,
             "options": [{"label": o} for o in options]}]}}]}}


def test_question_event_carries_orphaned_on_the_wire():
    w = ev.Question(question_id="q1", turn_id="t1", header="H", question="Q?",
                    options=[], multi_select=False, orphaned=True).to_wire()
    assert w["orphaned"] is True


def test_question_event_defaults_to_not_orphaned():
    """The live path must be untouched — every existing emitter omits the flag."""
    w = ev.Question(question_id="q1", turn_id="t1", header="H", question="Q?",
                    options=[], multi_select=False).to_wire()
    assert w["orphaned"] is False


def test_last_question_events_uses_replay_derived_ids(tmp_path):
    path = _write(tmp_path, [_ask("toolu_A", "Disposition", ["Close it", "Keep it"])])
    out = last_question_events(path)
    assert [q.question_id for q in out] == ["toolu_A-0"]
    assert out[0].header == "Disposition"
    assert [o["label"] for o in out[0].options] == ["Close it", "Keep it"]


def test_last_question_events_returns_only_the_most_recent_call(tmp_path):
    """A long session may have asked several times; only the one open at the
    restart is re-offered."""
    path = _write(tmp_path, [_ask("toolu_A", "First", ["a"]), _ask("toolu_B", "Second", ["b"])])
    out = last_question_events(path)
    assert [q.question_id for q in out] == ["toolu_B-0"]
    assert out[0].header == "Second"


def test_last_question_events_marks_them_orphaned(tmp_path):
    path = _write(tmp_path, [_ask("toolu_A", "Disposition", ["Close it"])])
    assert all(q.orphaned for q in last_question_events(path))


def test_last_question_events_is_empty_without_a_question(tmp_path):
    path = _write(tmp_path, [{"type": "assistant", "uuid": "u1", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}]}}])
    assert last_question_events(path) == []


def test_last_question_events_never_raises_on_a_missing_file(tmp_path):
    assert last_question_events(str(tmp_path / "nope.jsonl")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest server/tests/test_orphaned_question_replay.py -q -p no:randomly`
Expected: FAIL — `TypeError: Question.__init__() got an unexpected keyword argument 'orphaned'` and `ImportError: cannot import name 'last_question_events'`

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/events.py`, replace the `Question` dataclass with:

```python
@dataclass
class Question:
    question_id: str; turn_id: str; header: str; question: str; options: list[dict]; multi_select: bool = False
    ts: float | None = field(default_factory=_now_ms)
    # True when this question's waiter died with the daemon (see
    # server/sessions/orphaned_asks.py). The card is re-offered as answerable, but
    # answering sends a NEW prompt — there is no future left to resolve.
    orphaned: bool = False
    def to_wire(self): return {"type": "question", "questionId": self.question_id, "turnId": self.turn_id,
                               "header": self.header, "question": self.question, "options": self.options,
                               "multiSelect": self.multi_select, "ts": self.ts,
                               "orphaned": self.orphaned}
```

In `server/sessions/replay.py`, append at module level:

```python
def last_question_events(path: str) -> list["ev.Question"]:
    """Question events for the LAST AskUserQuestion tool_use in *path*, marked orphaned.

    Reuses the same reconstruction the full replay uses (``_assistant_block_events``)
    so the re-offer can never drift from what the transcript already rendered — in
    particular the ``f"{tool_use_id}-{index}"`` ids, which is what lets the client's
    reducer UPGRADE the card already on screen instead of appending a duplicate.

    Only the most recent call is returned: a long session may have asked several
    times, and only the one open at the restart is still awaiting an answer.
    Never raises — a missing or corrupt transcript yields [].
    """
    latest: list[ev.Question] = []
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                qs = [e for e in _assistant_block_events(
                          content, d.get("uuid"), d.get("timestamp"), None)
                      if isinstance(e, ev.Question)]
                if qs:
                    latest = qs
    except OSError:
        return []
    for q in latest:
        q.orphaned = True
    return latest
```

Check the top of `replay.py` for the existing `json` import and the exact name/signature of the block-events helper (it is `_assistant_block_events(content, uuid, ts, pid)` as of this writing); reuse them rather than adding new ones.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest server/tests/test_orphaned_question_replay.py -q -p no:randomly`
Expected: PASS (7 tests)

Then confirm nothing else depended on the old wire shape:
Run: `./.venv/bin/python -m pytest server/tests/ -q -p no:randomly -k "question or replay or ws_"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/sessions/replay.py server/tests/test_orphaned_question_replay.py
git commit -m "feat(sessions): reconstruct the last transcript question as orphaned"
```

---

### Task 3: Orchestrator — detect orphans and deliver an orphaned answer as a prompt

**Files:**
- Modify: `server/sessions/orchestrator.py` (`answer_question`, near line 7662; the autopilot call site near line 4834)
- Test: `server/tests/test_orphaned_answer_delivery.py`

**Interfaces:**
- Consumes: `orphaned_asks.orphaned_ask_kind`, `orphaned_asks.orphaned_answer_prompt`, `self.ensure_driver`, `self.send_prompt`.
- Produces:
  - `SessionOrchestrator.orphaned_interaction(session_id: str) -> str | None` — `"question"` / `"permission"` / `"plan"` / `None`.
  - `SessionOrchestrator.answer_question(...)` is now **async** and returns `bool`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orphaned_answer_delivery.py`:

```python
"""Answering a restart-orphaned question sends a NEW prompt.

There is no future left to resolve: the daemon's asyncio.Future and the SDK
subprocess that awaited it both died. The only honest delivery is the next turn's
prompt, carrying the selection and disowning the CLI's synthesised rejection.
"""
import asyncio
from datetime import datetime

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _rec(**over):
    base = dict(id="s1", name="s", account="a", claude_session_id="cs", cols=80, rows=24,
                cwd="/tmp", repo=None, branch=None, status="idle", name_is_auto=True,
                created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    base.update(over)
    return SessionRecord(**base)


def _orch(rec):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._drivers = {}
    return o


def test_orphaned_interaction_reports_the_kind():
    o = _orch(_rec(interrupted_by_restart=True, interrupted_from_status="waiting_question"))
    assert o.orphaned_interaction("s1") == "question"


def test_orphaned_interaction_is_none_for_a_healthy_session():
    assert _orch(_rec()).orphaned_interaction("s1") is None


def test_orphaned_interaction_is_none_for_an_unknown_session():
    assert _orch(_rec()).orphaned_interaction("nope") is None


def test_answering_an_orphaned_question_sends_a_prompt():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)
        sent = []

        async def _ensure(sid, identity=None):
            return object()

        async def _send(sid, text, attachment_ids=None):
            sent.append((sid, text))
            return "sent"

        o.ensure_driver = _ensure
        o.send_prompt = _send
        ok = await o.answer_question("s1", "q-0", ["Close #15205 as duplicate"], "tony", "option")

        assert ok is True
        assert len(sent) == 1
        assert "Close #15205 as duplicate" in sent[0][1]
        assert "restart" in sent[0][1].lower()

    asyncio.run(run())


def test_a_live_driver_still_wins_and_no_prompt_is_sent():
    """The live path must be untouched: orphan handling is strictly a fallback."""
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)
        sent = []

        class _D:
            def answer_question(self, qid, selected, identity, mode):
                return True

        o._drivers = {"s1": _D()}

        async def _send(sid, text, attachment_ids=None):
            sent.append(text)
            return "sent"

        o.send_prompt = _send
        assert await o.answer_question("s1", "q1", ["A"], "tony", "option") is True
        assert sent == [], "resolved a live future AND sent a duplicate prompt"

    asyncio.run(run())


def test_a_healthy_session_with_no_pending_question_still_returns_false():
    """An unresolvable answer must stay a rejection — the client shows
    QUESTION_NOT_PENDING. Only a genuinely orphaned session gets the fallback."""
    async def run():
        o = _orch(_rec())
        o._drivers = {}
        assert await o.answer_question("s1", "q1", ["A"], "tony", "option") is False

    asyncio.run(run())


def test_a_failed_send_reports_failure():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)

        async def _ensure(sid, identity=None):
            return object()

        async def _send(sid, text, attachment_ids=None):
            return "draining"

        o.ensure_driver = _ensure
        o.send_prompt = _send
        assert await o.answer_question("s1", "q-0", ["A"], "tony", "option") is False

    asyncio.run(run())


def test_no_driver_reports_failure():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
        o = _orch(rec)

        async def _ensure(sid, identity=None):
            return None

        o.ensure_driver = _ensure
        assert await o.answer_question("s1", "q-0", ["A"], "tony", "option") is False

    asyncio.run(run())


def test_an_orphaned_permission_does_not_take_the_question_fallback():
    """A permission cannot be answered with a selection — only re-asked (Task 5)."""
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_input")
        o = _orch(rec)
        sent = []

        async def _send(sid, text, attachment_ids=None):
            sent.append(text)
            return "sent"

        o.send_prompt = _send
        assert await o.answer_question("s1", "q1", ["A"], "tony", "option") is False
        assert sent == []

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest server/tests/test_orphaned_answer_delivery.py -q -p no:randomly`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'orphaned_interaction'`

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`, add near the top with the other `server.sessions` imports:

```python
from .orphaned_asks import orphaned_answer_prompt, orphaned_ask_kind
```

Replace `answer_question` (currently at ~line 7662) with:

```python
    def orphaned_interaction(self, session_id: str) -> str | None:
        """The kind of human-facing ask a restart orphaned for this session, or None.

        One accessor over the flags rehydrate() sets, so every caller (the answer
        fallback, the WS attach re-offer, the UI notice) asks the same question of
        the same source. See server/sessions/orphaned_asks.py.
        """
        rec = self._records.get(session_id)
        if rec is None:
            return None
        return orphaned_ask_kind(
            getattr(rec, "interrupted_by_restart", False),
            getattr(rec, "interrupted_from_status", None))

    async def answer_question(self, session_id, question_id, selected, identity,
                              mode="option") -> bool:
        """Resolve a pending AskUserQuestion with the user's selected answers and mode.

        Live path first, unchanged: resolve the driver's future. If that declines AND
        the session's question was orphaned by a restart, there is no future left to
        resolve — the honest delivery is a NEW prompt carrying the selection (see
        orphaned_answer_prompt, which also disowns the rejection the CLI synthesises
        for the dangling tool_use). Returns True if the answer reached the session.
        """
        d = self._drivers.get(session_id)
        if d is not None and d.answer_question(question_id, selected, identity, mode):
            return True
        if self.orphaned_interaction(session_id) != "question":
            return False
        return await self._deliver_orphaned_answer(session_id, selected, identity)

    async def _deliver_orphaned_answer(self, session_id: str, selected: list[str],
                                       identity: str) -> bool:
        """Send an orphaned question's answer as the next turn's prompt."""
        if await self.ensure_driver(session_id, identity) is None:
            log.warning("orphaned_answer: no driver for %s — answer not delivered",
                        session_id)
            return False
        result = await self.send_prompt(session_id, orphaned_answer_prompt(list(selected)))
        if result != "sent":
            log.warning("orphaned_answer: send_prompt returned %r for %s", result, session_id)
            return False
        log_event(log, logging.INFO, "session.orphaned_answer_delivered",
                  session_id=session_id, **{"selected": list(selected)})
        return True
```

Update the autopilot call site (~line 4834) — it is already inside an `async def`, so only `await` is added:

```python
            ok = bool(await self.answer_question(
                session_id, stuck["question_id"], selected, SYSTEM_IDENTITY, "option"))
```

Check `server/sessions/facade.py` and `server/sessions/remote.py` for an `answer_question` passthrough; the facade already wraps with `_maybe_await`, so an async method needs no change there, but confirm rather than assume.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest server/tests/test_orphaned_answer_delivery.py -q -p no:randomly`
Expected: PASS (9 tests)

Then the existing callers:
Run: `./.venv/bin/python -m pytest server/tests/ -q -p no:randomly -k "answer or autopilot or ws_commands"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orphaned_answer_delivery.py
git commit -m "fix(sessions): deliver an orphaned question's answer as a new prompt"
```

---

### Task 4: WS attach re-offers the orphaned question

**Files:**
- Modify: `server/bridge/ws.py:543-553` (immediately after the existing pending-question re-emit)
- Test: `server/tests/test_ws_orphaned_question_reoffer.py`

**Interfaces:**
- Consumes: `orchestrator.pending_questions`, `orchestrator.orphaned_interaction`, `orchestrator.transcript_path`, `replay.last_question_events`.
- Produces: a `question` frame with `"orphaned": true` on the attach stream.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ws_orphaned_question_reoffer.py`:

```python
"""On attach, a session whose question died with the daemon re-offers it.

Without this the replayed card stays live:false forever — PendingQuestionPanel
selects on `live`, so the only rendering is the read-only inline copy and the user
has no way to answer.
"""
import asyncio
import json

from server.bridge.ws import _orphaned_question_frames


def _write(tmp_path, tool_id, header):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "assistant", "uuid": "u1", "message": {
        "role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {
                "questions": [{"header": header, "question": "Q?", "multiSelect": False,
                               "options": [{"label": "Close it"}]}]}}]}}))
    return str(p)


class _Orch:
    def __init__(self, pending, kind, path):
        self._pending, self._kind, self._path = pending, kind, path

    async def pending_questions(self, sid):
        return self._pending

    def orphaned_interaction(self, sid):
        return self._kind

    def transcript_path(self, sid):
        return self._path


def test_reoffers_the_transcript_question_when_nothing_is_live(tmp_path):
    path = _write(tmp_path, "toolu_A", "Disposition")
    frames = asyncio.run(_orphaned_question_frames(_Orch([], "question", path), "s1"))
    assert len(frames) == 1
    assert frames[0]["type"] == "question"
    assert frames[0]["orphaned"] is True
    assert frames[0]["questionId"] == "toolu_A-0"
    assert frames[0]["header"] == "Disposition"


def test_no_reoffer_when_a_live_question_is_pending(tmp_path):
    """A live pending question is already re-emitted by the existing path; adding a
    second, orphaned copy of the same card would double it."""
    path = _write(tmp_path, "toolu_A", "Disposition")
    live = [{"question_id": "live-1", "turn_id": "t", "header": "H", "question": "Q",
             "options": [], "multi_select": False}]
    assert asyncio.run(_orphaned_question_frames(_Orch(live, "question", path), "s1")) == []


def test_no_reoffer_for_a_healthy_session(tmp_path):
    path = _write(tmp_path, "toolu_A", "Disposition")
    assert asyncio.run(_orphaned_question_frames(_Orch([], None, path), "s1")) == []


def test_no_reoffer_for_an_orphaned_permission(tmp_path):
    """A permission leaves no transcript record and cannot be answered by selection."""
    path = _write(tmp_path, "toolu_A", "Disposition")
    assert asyncio.run(_orphaned_question_frames(_Orch([], "permission", path), "s1")) == []


def test_no_transcript_is_not_an_error():
    assert asyncio.run(_orphaned_question_frames(_Orch([], "question", None), "s1")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest server/tests/test_ws_orphaned_question_reoffer.py -q -p no:randomly`
Expected: FAIL — `ImportError: cannot import name '_orphaned_question_frames'`

- [ ] **Step 3: Write minimal implementation**

In `server/bridge/ws.py`, add at module level (near the other module-level helpers):

```python
async def _orphaned_question_frames(orchestrator, session_id: str) -> list[dict]:
    """Wire frames re-offering a question whose waiter died with the daemon.

    Empty unless the session has NO live pending question (that path already
    re-emits, and a second copy would duplicate the card) and its pre-restart
    status was waiting_question. The ids come from replay, so the client's reducer
    upgrades the card already on screen rather than appending a duplicate.
    """
    if await orchestrator.pending_questions(session_id):
        return []
    if orchestrator.orphaned_interaction(session_id) != "question":
        return []
    path = orchestrator.transcript_path(session_id)
    if not path:
        return []
    from server.sessions.replay import last_question_events
    return [q.to_wire() for q in last_question_events(path)]
```

Then in `_handle_attach`, immediately after the existing `for q in await orchestrator.pending_questions(session_id):` loop, add:

```python
            # Re-offer a question whose waiter died with the daemon. The loop above
            # only ever finds LIVE questions; after a restart the registry is empty
            # by construction, so without this the replayed card stays read-only and
            # the session sits in an unknown state (see orphaned_asks.py).
            try:
                for frame in await _orphaned_question_frames(orchestrator, session_id):
                    out_q.put_nowait({**frame, "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: orphaned question re-offer failed for %s",
                            session_id, exc_info=True)
```

`orphaned_interaction` is sync on the orchestrator but may be a coroutine through the remote facade — if `server/sessions/remote.py` proxies it, add a passthrough there and `await` it via the facade's `_maybe_await` like its neighbours. Check both files and follow whichever pattern `pending_questions` uses.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest server/tests/test_ws_orphaned_question_reoffer.py -q -p no:randomly`
Expected: PASS (5 tests)

Run: `./.venv/bin/python -m pytest server/tests/ -q -p no:randomly -k "ws_"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws_orphaned_question_reoffer.py
git commit -m "feat(ws): re-offer a restart-orphaned question on attach"
```

---

### Task 5: Frontend — the orphaned question is answerable and says why

**Files:**
- Modify: `web/src/session/reducer.ts:24` (Block union) and `:698-716` (the question case)
- Modify: `web/src/components/session/QuestionCard.tsx`
- Test: `web/src/session/__tests__/reducer.orphanedQuestion.test.ts` (create)
- Test: `web/src/components/__tests__/QuestionCard.orphaned.test.tsx` (create)

**Interfaces:**
- Consumes: the `question` frame's `orphaned` boolean from Task 2.
- Produces: `Block` of kind `question` gains `orphaned?: boolean`; `QuestionCard` renders a banner with `data-testid="orphaned-question-notice"` when `block.orphaned` and not `readOnly`.

- [ ] **Step 1: Write the failing test**

Create `web/src/session/__tests__/reducer.orphanedQuestion.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'

const q = (over: Record<string, unknown> = {}) => ({
  type: 'question', questionId: 'toolu_A-0', turnId: 't1', header: 'Disposition',
  question: 'How should I dispose of #15205?', multiSelect: false,
  options: [{ label: 'Close it' }], ...over,
})

function questionBlock(state: ReturnType<typeof transcriptReducer>) {
  for (const t of state.turns) {
    for (const b of t.blocks) if (b.kind === 'question') return b
  }
  return null
}

describe('orphaned question', () => {
  it('a replayed question starts read-only', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    const b = questionBlock(s)
    expect(b?.kind === 'question' && b.live).toBeFalsy()
  })

  it('an orphaned re-offer upgrades the replayed card to live and marks it orphaned', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    s = transcriptReducer(s, { type: 'replayComplete' } as never)
    s = transcriptReducer(s, q({ orphaned: true }) as never)
    const b = questionBlock(s)
    expect(b?.kind === 'question' && b.live).toBe(true)
    expect(b?.kind === 'question' && b.orphaned).toBe(true)
  })

  it('does not append a duplicate card — the ids match replay', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    s = transcriptReducer(s, { type: 'replayComplete' } as never)
    s = transcriptReducer(s, q({ orphaned: true }) as never)
    const all = s.turns.flatMap((t) => t.blocks).filter((b) => b.kind === 'question')
    expect(all).toHaveLength(1)
  })

  it('never resurrects an already-answered question', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    s = transcriptReducer(s, { type: 'replayComplete' } as never)
    s = transcriptReducer(s, { type: 'questionResolved', questionId: 'toolu_A-0', turnId: 't1', selected: ['Close it'] } as never)
    s = transcriptReducer(s, q({ orphaned: true }) as never)
    const b = questionBlock(s)
    expect(b?.kind === 'question' && b.live).toBeFalsy()
  })
})
```

Create `web/src/components/__tests__/QuestionCard.orphaned.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QuestionCard } from '../session/QuestionCard'

const block = (over: Record<string, unknown> = {}) => ({
  kind: 'question' as const, id: 'toolu_A-0', questionId: 'toolu_A-0',
  header: 'Disposition', question: 'How should I dispose of #15205?',
  options: [{ label: 'Close it' }], multiSelect: false, live: true, ...over,
})

describe('QuestionCard orphaned notice', () => {
  it('explains that answering starts a new turn', () => {
    render(<QuestionCard block={block({ orphaned: true }) as never} onAnswer={vi.fn()} />)
    expect(screen.getByTestId('orphaned-question-notice')).toBeInTheDocument()
    expect(screen.getByTestId('orphaned-question-notice').textContent?.toLowerCase())
      .toContain('restart')
  })

  it('shows no notice for a normal live question', () => {
    render(<QuestionCard block={block() as never} onAnswer={vi.fn()} />)
    expect(screen.queryByTestId('orphaned-question-notice')).toBeNull()
  })

  it('keeps the options clickable so the answer can actually be given', async () => {
    const onAnswer = vi.fn()
    render(<QuestionCard block={block({ orphaned: true }) as never} onAnswer={onAnswer} />)
    screen.getByRole('button', { name: /Close it/ }).click()
    expect(onAnswer).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm --prefix web exec -- vitest run src/session/__tests__/reducer.orphanedQuestion.test.ts src/components/__tests__/QuestionCard.orphaned.test.tsx`
Expected: FAIL — the orphaned re-offer does not set `orphaned`, and no `orphaned-question-notice` element exists.

- [ ] **Step 3: Write minimal implementation**

In `web/src/session/reducer.ts`, extend the question member of the `Block` union (line 24) with `orphaned?: boolean`:

```ts
  | { kind: 'question';   id: string; questionId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean; live?: boolean; orphaned?: boolean; answered?: string[]; ts?: number }
```

In the `case 'question':` block, destructure `orphaned` from the event and carry it through BOTH the upgrade branch and the new-block branch:

```ts
      const { questionId, turnId, header, question, options, multiSelect, orphaned } = ev
```

Upgrade branch — stamp `orphaned` alongside `live`:

```ts
            blocks: t.blocks.map((b) =>
              b.kind === 'question' && b.questionId === questionId
                ? { ...b, live: true, orphaned: !!orphaned }
                : b,
            ),
```

New-block branch:

```ts
      const newBlock: Block = { kind: 'question', id: questionId, questionId, header, question, options, multiSelect, live: !state.replaying, orphaned: !!orphaned, ts: ev.ts }
```

Add `orphaned?: boolean` to the `question` event type wherever `web/src/session/events.ts` declares the incoming frame shape.

In `web/src/components/session/QuestionCard.tsx`, inside the rendered card and above the options — only when `block.orphaned && !readOnly`:

```tsx
{block.orphaned && !readOnly && (
  <div
    data-testid="orphaned-question-notice"
    className="mb-2 rounded border border-amber-300 dark:border-amber-700 bg-amber-100/70 dark:bg-amber-900/30 px-2 py-1.5 text-xs text-amber-900 dark:text-amber-200"
  >
    A daemon restart interrupted this question, so the original turn is gone.
    Your choice will be sent as a new message and the session will continue from
    there.
  </div>
)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm --prefix web exec -- vitest run src/session/__tests__/reducer.orphanedQuestion.test.ts src/components/__tests__/QuestionCard.orphaned.test.tsx`
Expected: PASS (7 tests)

Run: `npm --prefix web exec -- vitest run src/session src/components`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.ts web/src/session/events.ts web/src/components/session/QuestionCard.tsx web/src/session/__tests__/reducer.orphanedQuestion.test.ts web/src/components/__tests__/QuestionCard.orphaned.test.tsx
git commit -m "feat(web): make a restart-orphaned question answerable again"
```

---

### Task 6: Orphaned permission / plan say so instead of vanishing

**Files:**
- Create: `web/src/components/session/InterruptedAskNotice.tsx`
- Modify: `web/src/components/session/SessionBody.tsx` (render it beside `PendingPermissionPanel`, ~line 221)
- Modify: `web/src/api/types.ts` (declare `interruptedFromStatus` — already sent by `server/sessions/types.py:185`)
- Test: `web/src/components/__tests__/InterruptedAskNotice.test.tsx` (create)

**Interfaces:**
- Consumes: `Session.interruptedFromStatus` (`'waiting_input' | 'ready_review' | …`), and an `onAskAgain: () => void` supplied by `SessionBody` that sends a prompt.
- Produces: `InterruptedAskNotice({ interruptedFromStatus, onAskAgain })`, `data-testid="interrupted-ask-notice"`.

Rationale for the different remedy: a permission request leaves **no transcript record** (`can_use_tool` is a live callback), and a plan approval replays only as a raw `ExitPlanMode` tool card. Neither can be re-offered as a choice the way a question can, so the honest affordance is to say the prompt was lost and offer to have the assistant ask again.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/InterruptedAskNotice.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InterruptedAskNotice } from '../session/InterruptedAskNotice'

describe('InterruptedAskNotice', () => {
  it('explains a lost permission prompt', () => {
    render(<InterruptedAskNotice interruptedFromStatus="waiting_input" onAskAgain={vi.fn()} />)
    const el = screen.getByTestId('interrupted-ask-notice')
    expect(el.textContent?.toLowerCase()).toContain('permission')
    expect(el.textContent?.toLowerCase()).toContain('restart')
  })

  it('explains a lost plan approval', () => {
    render(<InterruptedAskNotice interruptedFromStatus="ready_review" onAskAgain={vi.fn()} />)
    expect(screen.getByTestId('interrupted-ask-notice').textContent?.toLowerCase())
      .toContain('plan')
  })

  it('asks again on click', () => {
    const onAskAgain = vi.fn()
    render(<InterruptedAskNotice interruptedFromStatus="waiting_input" onAskAgain={onAskAgain} />)
    screen.getByRole('button', { name: /ask again/i }).click()
    expect(onAskAgain).toHaveBeenCalledTimes(1)
  })

  it('renders nothing for a question (QuestionCard owns that case)', () => {
    const { container } = render(
      <InterruptedAskNotice interruptedFromStatus="waiting_question" onAskAgain={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a healthy session', () => {
    const { container } = render(
      <InterruptedAskNotice interruptedFromStatus={null} onAskAgain={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix web exec -- vitest run src/components/__tests__/InterruptedAskNotice.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/components/session/InterruptedAskNotice.tsx`:

```tsx
import { cn } from '@/lib/utils'

interface Props {
  /** Pre-restart status from the server (Session.interruptedFromStatus). */
  interruptedFromStatus?: string | null
  onAskAgain: () => void
}

const COPY: Record<string, string> = {
  waiting_input:
    'A daemon restart interrupted a permission request, so the prompt is gone and the '
    + 'turn it belonged to has ended. Nothing was approved.',
  ready_review:
    'A daemon restart interrupted a plan approval, so the prompt is gone and the turn '
    + 'it belonged to has ended. Nothing was approved.',
}

/**
 * A permission or plan approval whose waiter died with the daemon.
 *
 * Unlike a question — which is a real AskUserQuestion tool_use and can be re-offered
 * from the transcript — a permission leaves NO transcript record (can_use_tool is a
 * live callback) and a plan replays only as a raw ExitPlanMode tool card. Neither can
 * be re-presented as a choice, so the honest affordance is to say the prompt was lost
 * and offer to have the assistant ask again.
 */
export function InterruptedAskNotice({ interruptedFromStatus, onAskAgain }: Props) {
  const copy = COPY[interruptedFromStatus ?? '']
  if (!copy) return null
  return (
    <div
      data-testid="interrupted-ask-notice"
      role="status"
      className={cn(
        'flex-shrink-0 border-t border-amber-200 dark:border-amber-800',
        'bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2 text-xs',
        'text-amber-900 dark:text-amber-200 flex items-center gap-3',
      )}
    >
      <span className="flex-1">{copy}</span>
      <button
        type="button"
        onClick={onAskAgain}
        className={cn(
          'flex-shrink-0 rounded border border-amber-400 dark:border-amber-600',
          'px-2 py-1 font-medium hover:bg-amber-100 dark:hover:bg-amber-900/40',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
        )}
      >
        Ask again
      </button>
    </div>
  )
}
```

In `web/src/api/types.ts`, add to the `Session` interface next to `resumable`:

```ts
  /** Pre-restart status when a daemon restart interrupted this session; drives the
   *  orphaned-ask notice. Sent by server/sessions/types.py:to_dict. */
  interruptedFromStatus?: string | null
```

In `web/src/components/session/SessionBody.tsx`, render it immediately before `<PendingPermissionPanel …>`:

```tsx
            <InterruptedAskNotice
              interruptedFromStatus={session?.interruptedFromStatus}
              onAskAgain={() => rt.onPrompt(
                'The permission or plan approval you were waiting on was lost when the '
                + 'daemon restarted — I never saw it. Please re-request whatever you need '
                + 'approved, or tell me where things stand.',
              )}
            />
```

Import it at the top, and resolve `session` from the `useSessions()` data already available in this component (it is used for the model/account rows) — reuse that lookup rather than adding a second fetch. The runtime's prompt-send callback is `rt.onPrompt` (verified against `useSessionRuntime`'s return object); check its exact signature and adapt the call if it takes more than the text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm --prefix web exec -- vitest run src/components/__tests__/InterruptedAskNotice.test.tsx`
Expected: PASS (5 tests)

Run: `npm --prefix web exec -- tsc -b web/tsconfig.json --force`
Expected: no output (clean)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/InterruptedAskNotice.tsx web/src/components/session/SessionBody.tsx web/src/api/types.ts web/src/components/__tests__/InterruptedAskNotice.test.tsx
git commit -m "feat(web): tell the user when a restart lost a permission or plan prompt"
```

---

### Task 7: Full gate + audit note

**Files:**
- Create: `docs/audits/2026-09-03-restart-orphaned-asks-audit.md`

- [ ] **Step 1: Run the complete gate**

```bash
./.venv/bin/python -m ruff check src/ server/
./.venv/bin/python -m mypy server/ --ignore-missing-imports
./.venv/bin/python -m pytest tests/ -q
./.venv/bin/python -m pytest server/tests/ -q -p no:randomly
npm --prefix web run lint
npm --prefix web run test
npm --prefix web exec -- tsc -b web/tsconfig.json --force
```

Expected: all green. Do NOT edit any source file while `pytest server/tests/` is running — `test_cicd_orchestrator_wiring.py` uses `inspect.getsource`, which reads stale `linecache` offsets and fails spuriously if a file's line numbers shift mid-run.

- [ ] **Step 2: Write the audit note**

Create `docs/audits/2026-09-03-restart-orphaned-asks-audit.md` with the headings **Symptom / Anti-pattern / Fix shape / Where to look / Reference**, recording: the anti-pattern ("a human-facing request modelled ONLY as a live in-process future, with UI affordance gated on it"), the three instances, the prod evidence above, and the sweep result for other future-backed registries (`QuestionRegistry`, `PermissionRegistry` — both now covered; note `_grants` is a TTL cache whose loss is harmless).

- [ ] **Step 3: Commit and open the PR**

```bash
git add docs/audits/2026-09-03-restart-orphaned-asks-audit.md docs/superpowers/plans/2026-09-03-restart-orphaned-interactive-requests.md
git commit -m "docs: record the restart-orphaned interactive request audit"
git push -u origin <branch>
gh pr create --title "fix(sessions): recover restart-orphaned questions, permissions and plan approvals" --body-file <file>
gh pr merge <#> --squash --auto
```

---

## Self-Review

**Spec coverage.** Every failure mode in the Problem table has a task: `waiting_question` → Tasks 1-5; `waiting_input` and `ready_review` → Task 6; the false-rejection correction → Task 1 (`orphaned_answer_prompt`) with a dedicated assertion; autopilot's silently-failing `answer_question` → Task 3 (fixed by the same fallback, call site updated).

**Placeholder scan.** Every code step carries real code. The three "confirm against the file" notes (the `replay.py` helper name, the `remote.py`/`facade.py` passthrough, and `SessionBody`'s send callback) are deliberate verification instructions, not deferred decisions — each names the exact file and the exact thing to check.

**Type consistency.** `orphaned_ask_kind` returns `"question" | "permission" | "plan" | None` in Task 1 and is consumed under those exact literals in Tasks 3, 4 and 6. `orphaned_interaction` is the orchestrator accessor name in Tasks 3 and 4. `last_question_events` is defined in Task 2 and consumed in Task 4. The reducer's `orphaned?: boolean` (Task 5) matches the wire key `"orphaned"` (Task 2). `interruptedFromStatus` (Task 6) matches `server/sessions/types.py:185`.

**Known gap, deliberately out of scope.** An orphaned question whose transcript already carries the CLI's synthesised rejection is re-offered identically to one that does not — we do not attempt to distinguish a genuine user dismissal from a synthesised one, because the transcript records them identically. The `interrupted_by_restart` flag is the discriminator instead, and it is only ever set when the daemon actually died mid-ask.
