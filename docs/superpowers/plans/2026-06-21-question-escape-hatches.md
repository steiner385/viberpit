# Question Escape Hatches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `AskUserQuestion` card in a ccswitch session always offers two escape hatches — **write a free-form response** (inline) and **chat about this** (routes to the composer) — in addition to Claude's predefined options.

**Architecture:** Claude's `AskUserQuestion` is already intercepted server-side (`driver.py:_handle_ask_user_question`) and rendered as a `QuestionCard`; answers flow back through `answerQuestion` → `QuestionRegistry.resolve`, resolving a blocked `asyncio.Future` whose result becomes the tool-result text the model sees. This plan threads an answer **`mode`** (`'option' | 'freeform' | 'chat'`) end-to-end so the model receives intent-appropriate framing, adds the two UI affordances to `QuestionCard`, and wires a one-shot "armed for chat" pointer in `SessionView` so the next composer message resolves the pending question instead of starting a new prompt.

**Tech Stack:** Python (FastAPI, asyncio, pytest) backend · React + TypeScript + Tailwind + Vitest/Testing Library frontend.

## Global Constraints

- **TDD, every task:** write the failing test first, watch it fail, implement minimally, watch it pass, commit. (Constitution Principle V.)
- **Backend tests run via the main checkout's prebuilt venv** — this worktree has no built env. Use `<main-checkout>/.venv/bin/python -m pytest server/tests/…`. For a full-suite run that touches auth, wrap with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. The tasks below touch only question/driver/orchestrator tests (no HTTP auth), so a plain `python -m pytest <file>` against that venv is sufficient.
- **Frontend tests run in `web/`** with `web/node_modules` symlinked from the main checkout: `cd web && npx vitest run <file>`, plus `npx tsc --noEmit` and `npx eslint` before completion.
- **`mode` is optional and defaults to `'option'`** at every layer (wire, registry, driver, orchestrator) so all existing click-an-option and dismissal flows are unchanged.
- **No DB/schema changes.** Question responses stay in-flight / UI-managed.
- **Branch off `master`** (already on `001-question-escape-hatches`); commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Scope = `AskUserQuestion` cards only.** Do not touch `PermissionCard` / `ExitPlanMode`.

---

## File Structure

**Backend (modify):**
- `server/sessions/questions.py` — `QuestionRegistry.resolve` gains `mode`; future resolves to `(selected, mode)` tuple (dismissed stays `None`).
- `server/sessions/driver.py` — `answer_question` gains `mode`; `_handle_ask_user_question` unpacks `(selected, mode)` and formats the deny message per mode (two new static helpers).
- `server/sessions/orchestrator.py` — `answer_question` gains `mode`, forwards it.
- `server/bridge/ws.py` — parse + validate `mode` from the `answerQuestion` frame.

**Backend (tests, modify/add):**
- `server/tests/test_questions.py`, `server/tests/test_orchestrator_questions.py` — update assertions for the tuple result; add mode tests.
- `server/tests/test_driver.py` — add freeform + chat framing tests.

**Frontend (modify):**
- `web/src/ws/client.ts` — `answerQuestion(…, mode = 'option')` includes `mode` on the wire; hook wrapper updated.
- `web/src/components/session/QuestionCard.tsx` — inline free-form textarea, "Chat about this" button, armed-for-chat banner.
- `web/src/components/session/Turn.tsx`, `web/src/components/session/TranscriptView.tsx` — thread `onAnswer` (with `mode`), `onChat`, `armedQuestionId`.
- `web/src/components/session/Composer.tsx` — `chatArmed` prop changes the placeholder.
- `web/src/components/session/SessionView.tsx` — `armedQuestionId` state, prompt routing, `handleAnswer` mode, clear-on-interrupt, defensive cleanup, prop wiring.

**Frontend (tests, modify/add):**
- `web/src/ws/client.test.ts` — update the `answerQuestion` shape assertion; add a chat-mode case.
- `web/src/components/session/__tests__/QuestionCard.test.tsx` — escape-hatch tests.
- `web/src/components/session/__tests__/SessionView.test.tsx` — chat-routing + free-form integration tests.

---

## Task 1: Backend — `QuestionRegistry` carries answer mode

**Files:**
- Modify: `server/sessions/questions.py:66-78` (`resolve`)
- Test: `server/tests/test_questions.py`, `server/tests/test_orchestrator_questions.py:104-108`

**Interfaces:**
- Produces: `QuestionRegistry.resolve(question_id: str, selected: list[str], identity: str, mode: str = "option") -> bool`. On success the registered future resolves to the tuple `(selected, mode)`. `deny_all` still resolves pending futures to `None` (dismissed sentinel — unchanged).

- [ ] **Step 1: Update the existing future-result assertions to the tuple shape (these will fail until Step 3).**

In `server/tests/test_questions.py`, change the three result assertions:

```python
def test_request_resolve_sets_future_to_selected_list():
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        assert qid is not None
        reg.resolve(qid, ["Option A"], "me")
        return await fut
    result = asyncio.run(run())
    assert result == (["Option A"], "option")


def test_request_resolve_multi_select():
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg, multi_select=True)
        reg.resolve(qid, ["A", "B"], "me")
        return await fut
    result = asyncio.run(run())
    assert result == (["A", "B"], "option")
```

And in `test_multiple_questions_same_session`:

```python
    r1, r2 = asyncio.run(run())
    assert r1 == (["First"], "option")
    assert r2 == (["Second"], "option")
```

- [ ] **Step 2: Add a new test for the mode being carried.**

Append to `server/tests/test_questions.py`:

```python
def test_resolve_carries_mode_to_future():
    """resolve(..., mode='chat') resolves the future to (selected, mode)."""
    reg = QuestionRegistry()
    async def run():
        qid, fut = _req(reg)
        ok = reg.resolve(qid, ["typed text"], "me", mode="chat")
        return ok, await fut
    ok, result = asyncio.run(run())
    assert ok is True
    assert result == (["typed text"], "chat")
```

- [ ] **Step 3: Run the question tests to verify they fail.**

Run: `python -m pytest server/tests/test_questions.py -v`
Expected: FAIL — `test_resolve_carries_mode_to_future` errors (unexpected `mode` kwarg) and the tuple assertions fail (`["Option A"] != (["Option A"], "option")`).

- [ ] **Step 4: Implement the `mode` parameter and tuple result.**

In `server/sessions/questions.py`, replace `resolve`:

```python
    def resolve(self, question_id: str, selected: list[str], identity: str,
                mode: str = "option") -> bool:
        """Resolve a pending question with the user's selection and answer mode.

        ``mode`` is one of "option" (picked a predefined option), "freeform"
        (typed their own answer to this question), or "chat" (chose to discuss
        rather than pick). The future resolves to the tuple ``(selected, mode)``;
        ``deny_all`` instead resolves to ``None`` (dismissed sentinel).

        Returns True if resolved, False if unknown id, identity mismatch, or already done.
        """
        rec = self._pending.get(question_id)
        if rec is None or rec["identity"] != identity:
            return False
        del self._pending[question_id]
        if not rec["future"].done():
            rec["future"].set_result((selected, mode))
            return True
        return False
```

Also update the class docstring line and `request`'s docstring note that the future resolves to `(selected, mode)` or `None`:

```python
    """Lifecycle analogue to PermissionRegistry, but for AskUserQuestion.

    The future resolves to the tuple (selected_labels, mode), or None if the
    question was dismissed (via deny_all on disconnect/interrupt).
    """
```

- [ ] **Step 5: Update the orchestrator delegation test (it awaits the future directly).**

In `server/tests/test_orchestrator_questions.py`, change the resolved-value assertion in `test_answer_question_delegates_to_driver`:

```python
    resolved = client.portal.call(_check)
    assert resolved == (["Option X"], "option")
```

- [ ] **Step 6: Run both files to verify they pass.**

Run: `python -m pytest server/tests/test_questions.py server/tests/test_orchestrator_questions.py -v`
Expected: PASS (all).

- [ ] **Step 7: Commit.**

```bash
git add server/sessions/questions.py server/tests/test_questions.py server/tests/test_orchestrator_questions.py
git commit -m "feat(questions): carry answer mode through QuestionRegistry future

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Backend — driver framing by mode + orchestrator passthrough

**Files:**
- Modify: `server/sessions/driver.py:74-147` (`_handle_ask_user_question`, `answer_question`)
- Modify: `server/sessions/orchestrator.py:592-599` (`answer_question`)
- Test: `server/tests/test_driver.py`

**Interfaces:**
- Consumes: `QuestionRegistry.resolve(qid, selected, identity, mode="option")` and the future result `(selected, mode) | None` (from Task 1).
- Produces:
  - `SessionDriver.answer_question(question_id: str, selected: list[str], identity: str, mode: str = "option") -> bool`
  - `SessionOrchestrator.answer_question(session_id, question_id, selected, identity, mode="option") -> bool`
  - Deny-message framing: `freeform` → `"[User answered in their own words] {Q} → {text}"`; `chat` → `"[User wants to discuss rather than pick an option] {text}"`; `option` (default) → existing `"[User answered via the question UI] {Q} → {answer}"`.

- [ ] **Step 1: Write failing tests for freeform and chat framing.**

Append to `server/tests/test_driver.py` (mirror `test_askuserquestion_blocks_…`):

```python
def test_askuserquestion_freeform_framing():
    """A freeform answer is framed as the user's own words."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-ff", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        tool_input = {"questions": [{"header": "Choose", "question": "Favourite?",
                                     "options": [{"label": "Option A"}], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        q_event = d._out.get_nowait()
        assert isinstance(q_event, Question)

        d.answer_question(q_event.question_id, ["I prefer C, actually"], "me", mode="freeform")
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "[User answered in their own words]" in result.message
        assert "I prefer C, actually" in result.message

    asyncio.run(run())


def test_askuserquestion_chat_framing():
    """A chat answer is framed as a discussion redirect, not a rigid selection."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-chat", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        tool_input = {"questions": [{"header": "Choose", "question": "Favourite?",
                                     "options": [{"label": "Option A"}], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        q_event = d._out.get_nowait()
        assert isinstance(q_event, Question)

        d.answer_question(q_event.question_id, ["why are you asking this?"], "me", mode="chat")
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "[User wants to discuss rather than pick an option]" in result.message
        assert "why are you asking this?" in result.message

    asyncio.run(run())
```

- [ ] **Step 2: Run the new tests to verify they fail.**

Run: `python -m pytest server/tests/test_driver.py -k "freeform_framing or chat_framing" -v`
Expected: FAIL — `answer_question()` got an unexpected keyword argument `mode`.

- [ ] **Step 3: Add the framing helpers and thread `mode` through the driver.**

In `server/sessions/driver.py`, add two static helpers next to `_normalize_option` (after line 72):

```python
    @staticmethod
    def _format_answer_message(q_text: str, selected: list[str], mode: str) -> str:
        """Single-question deny-message body, framed by answer mode."""
        answer_str = ", ".join(selected)
        if mode == "chat":
            return f"[User wants to discuss rather than pick an option] {answer_str}"
        if mode == "freeform":
            return f"[User answered in their own words] {q_text} → {answer_str}"
        return f"[User answered via the question UI] {q_text} → {answer_str}"

    @staticmethod
    def _format_answer_line(q_text: str, selected: list[str], mode: str) -> str:
        """Per-question line for the multi-question deny message, framed by mode."""
        answer_str = ", ".join(selected)
        if mode == "chat":
            return f"- {q_text} → (discussing) {answer_str}"
        if mode == "freeform":
            return f"- {q_text} → (in their own words) {answer_str}"
        return f"- {q_text} → {answer_str}"
```

Replace the answer-collection and message-building block in `_handle_ask_user_question` (lines 112-143) with:

```python
        # Await all answers concurrently (gather preserves order).
        # Each future resolves to (selected, mode) or None (dismissed).
        futs = [fut for _, fut in pending]
        answers: list[tuple[list[str], str] | None] = await asyncio.gather(*futs)

        # Question(s) resolved → back to in_progress.
        await self._emit_dashboard_status()

        # Build the deny message.
        questions_list = [q for q, _ in pending]
        if len(questions_list) == 1:
            q_text = questions_list[0].get("question") or ""
            answer = answers[0]
            if answer is None:
                msg = f"[User dismissed the question UI] {q_text} → (dismissed — user did not answer)"
            else:
                selected, mode = answer
                msg = self._format_answer_message(q_text, selected, mode)
        else:
            all_dismissed = all(a is None for a in answers)
            prefix = "[User dismissed the question UI]" if all_dismissed else "[User answered via the question UI]"
            lines = [prefix]
            for q_obj, answer in zip(questions_list, answers):
                q_text = q_obj.get("question") or ""
                if answer is None:
                    lines.append(f"- {q_text} → (dismissed — user did not answer)")
                else:
                    selected, mode = answer
                    lines.append(self._format_answer_line(q_text, selected, mode))
            msg = "\n".join(lines)

        return PermissionResultDeny(message=msg)
```

Update `answer_question` (lines 145-147):

```python
    def answer_question(self, question_id: str, selected: list[str], identity: str,
                        mode: str = "option") -> bool:
        """Resolve a pending question with the user's selection and mode. Mirrors resolve_permission."""
        return self._qreg.resolve(question_id, selected, identity, mode)
```

- [ ] **Step 4: Run the driver tests (new + existing question tests) to verify they pass.**

Run: `python -m pytest server/tests/test_driver.py -k "askuserquestion" -v`
Expected: PASS — including the existing `…blocks…`, `…dismiss…`, `…multi_question…`, `…options_normalized…` tests (option-mode framing is byte-identical to before).

- [ ] **Step 5: Thread `mode` through the orchestrator.**

In `server/sessions/orchestrator.py`, replace `answer_question` (lines 592-599):

```python
    def answer_question(self, session_id, question_id, selected, identity, mode="option") -> bool:
        """Resolve a pending AskUserQuestion with the user's selected answers and mode.

        Returns True if resolved, False if no driver for session_id or unknown question_id.
        Mirrors resolve_permission.
        """
        d = self._drivers.get(session_id)
        return d.answer_question(question_id, selected, identity, mode) if d else False
```

- [ ] **Step 6: Run the orchestrator question tests to verify still green.**

Run: `python -m pytest server/tests/test_orchestrator_questions.py -v`
Expected: PASS (default `mode="option"` keeps existing behavior).

- [ ] **Step 7: Commit.**

```bash
git add server/sessions/driver.py server/sessions/orchestrator.py server/tests/test_driver.py
git commit -m "feat(driver): frame question answers by mode (freeform/chat/option)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Backend — WS bridge parses and validates `mode`

**Files:**
- Modify: `server/bridge/ws.py:298-309` (the `answerQuestion` branch)
- Test: `server/tests/test_orchestrator_questions.py`

**Interfaces:**
- Consumes: `SessionOrchestrator.answer_question(session_id, question_id, selected, identity, mode)` (Task 2).
- Produces: the `answerQuestion` WS frame now reads `msg["mode"]`, coerces any value outside `{"option","freeform","chat"}` to `"option"`, and forwards it. Audit payload records the mode.

- [ ] **Step 1: Write a failing test that the bridge forwards a validated mode.**

Append to `server/tests/test_orchestrator_questions.py`:

```python
def test_answer_question_forwards_freeform_mode(orch_env):
    """orchestrator.answer_question(..., mode='freeform') resolves the future with that mode."""
    orch, client = orch_env

    session = client.portal.call(lambda: orch.create_session("q-mode-test", identity="me"))
    session_id = session.id
    driver = orch.driver(session_id)
    assert driver is not None

    async def _register():
        return driver._qreg.request(session_id, "t1", "H", "Q?", [{"label": "X"}], False, "me")

    qid, fut = client.portal.call(_register)

    assert orch.answer_question(session_id, qid, ["my words"], "me", mode="freeform") is True

    async def _check():
        return await fut

    assert client.portal.call(_check) == (["my words"], "freeform")
    client.portal.call(orch.close_session, session_id)
```

- [ ] **Step 2: Run it to verify it passes already at the orchestrator layer.**

Run: `python -m pytest server/tests/test_orchestrator_questions.py -k forwards_freeform_mode -v`
Expected: PASS (orchestrator already accepts `mode` from Task 2). This locks the orchestrator contract; the WS edit below adds validation at the wire boundary.

- [ ] **Step 3: Update the WS `answerQuestion` branch to parse + validate `mode`.**

In `server/bridge/ws.py`, replace lines 298-309:

```python
                elif msg_type == "answerQuestion":
                    question_id = msg.get("questionId", "")
                    selected = msg.get("selected", [])
                    mode = msg.get("mode", "option")
                    if mode not in ("option", "freeform", "chat"):
                        mode = "option"
                    orchestrator.answer_question(session_id, question_id, selected, identity, mode)
                    try:
                        async with db_session() as db:
                            await record_audit(db, "question.answer", identity,
                                               {"sessionId": session_id, "questionId": question_id,
                                                "selected": selected, "mode": mode})
                    except Exception:
                        log.warning("ws.audit_failed event=%s session=%s",
                                    "question.answer", session_id, exc_info=True)
```

- [ ] **Step 4: Run the backend question/driver/ws suite to verify green.**

Run: `python -m pytest server/tests/test_questions.py server/tests/test_driver.py server/tests/test_orchestrator_questions.py server/tests/test_ws.py -v`
Expected: PASS (all). The existing `test_ws.py` `answerQuestion` test omits `mode`, so it defaults to `"option"` — unchanged behavior.

- [ ] **Step 5: Commit.**

```bash
git add server/bridge/ws.py server/tests/test_orchestrator_questions.py
git commit -m "feat(ws): accept and validate answerQuestion mode

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Frontend — `answerQuestion` carries `mode` on the wire

**Files:**
- Modify: `web/src/ws/client.ts:151-153` (method), `:321` (hook wrapper)
- Test: `web/src/ws/client.test.ts:116-124`

**Interfaces:**
- Produces: `SessionSocket.answerQuestion(sessionId: string, questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat')` and the hook wrapper of the same signature. The frame is `{ type: 'answerQuestion', sessionId, questionId, selected, mode }` with `mode` defaulting to `'option'`.

- [ ] **Step 1: Update the existing shape test and add a chat-mode case (will fail).**

In `web/src/ws/client.test.ts`, update the assertion at line ~124 and add a second case:

```ts
  it('socket.answerQuestion("s","q1",["a"]) sends mode "option" by default', () => {
    const sock = lastSock()
    sock.open()
    sock.answerQuestion('s', 'q1', ['a'])
    const sent = parseSent(sock)
    expect(sent).toEqual({ type: 'answerQuestion', sessionId: 's', questionId: 'q1', selected: ['a'], mode: 'option' })
  })

  it('socket.answerQuestion forwards an explicit mode', () => {
    const sock = lastSock()
    sock.open()
    sock.answerQuestion('s', 'q1', ['hello'], 'chat')
    const sent = parseSent(sock)
    expect(sent).toEqual({ type: 'answerQuestion', sessionId: 's', questionId: 'q1', selected: ['hello'], mode: 'chat' })
  })
```

(Keep the original test's `beforeEach`/setup; only the body of the existing `answerQuestion` test changes, and the new test is added beside it.)

- [ ] **Step 2: Run to verify failure.**

Run: `cd web && npx vitest run src/ws/client.test.ts`
Expected: FAIL — sent frame lacks `mode`; second test fails to compile/assert.

- [ ] **Step 3: Add the `mode` parameter to the method and hook wrapper.**

In `web/src/ws/client.ts`, replace the method (lines 151-153):

```ts
  answerQuestion(sessionId: string, questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') {
    this._send({ type: 'answerQuestion', sessionId, questionId, selected, mode })
  }
```

And the hook wrapper (line 321):

```ts
  const answerQuestion      = useCallback((sessionId: string, questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') => socket.answerQuestion(sessionId, questionId, selected, mode), [socket])
```

- [ ] **Step 4: Run to verify pass.**

Run: `cd web && npx vitest run src/ws/client.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add web/src/ws/client.ts web/src/ws/client.test.ts
git commit -m "feat(ws-client): answerQuestion carries answer mode

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend — `QuestionCard` escape hatches

**Files:**
- Modify: `web/src/components/session/QuestionCard.tsx` (whole file)
- Test: `web/src/components/session/__tests__/QuestionCard.test.tsx`

**Interfaces:**
- Produces: `QuestionCard` props become `{ block, onAnswer: (questionId, selected, mode?) => void, onChat?: (questionId) => void, armedForChat?: boolean }`. Picking a predefined option calls `onAnswer(qid, [label])` (mode omitted → `'option'`). "Write a response" → inline textarea → `onAnswer(qid, [text], 'freeform')` (empty ignored). "Chat about this" → `onChat(qid)`. When `armedForChat`, options collapse to a banner with a Cancel button that calls `onChat(qid)`.

- [ ] **Step 1: Write failing tests for the escape hatches.**

Append to `web/src/components/session/__tests__/QuestionCard.test.tsx`:

```ts
  it('renders both escape hatches for single-select', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onChat={vi.fn()} />)
    expect(screen.getByRole('button', { name: /write a response/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /chat about this/i })).toBeInTheDocument()
  })

  it('renders both escape hatches for multi-select', () => {
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} onChat={vi.fn()} />)
    expect(screen.getByRole('button', { name: /write a response/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /chat about this/i })).toBeInTheDocument()
  })

  it('free-form: typing and sending calls onAnswer with mode "freeform"', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} onChat={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'my own answer')
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(onAnswer).toHaveBeenCalledWith('q-1', ['my own answer'], 'freeform')
  })

  it('free-form: empty input does not submit', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} onChat={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('chat: clicking "Chat about this" calls onChat', async () => {
    const user = userEvent.setup()
    const onChat = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onChat={onChat} />)
    await user.click(screen.getByRole('button', { name: /chat about this/i }))
    expect(onChat).toHaveBeenCalledWith('q-1')
  })

  it('armedForChat: options collapse to a banner and Cancel calls onChat', async () => {
    const user = userEvent.setup()
    const onChat = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onChat={onChat} armedForChat />)
    expect(screen.queryByRole('radio', { name: 'Option A' })).not.toBeInTheDocument()
    expect(screen.getByText(/replying in chat/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onChat).toHaveBeenCalledWith('q-1')
  })
```

- [ ] **Step 2: Run to verify failure.**

Run: `cd web && npx vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: FAIL — new buttons / banner not found.

- [ ] **Step 3: Rewrite `QuestionCard.tsx` with the escape hatches.**

Replace the entire file `web/src/components/session/QuestionCard.tsx` with:

```tsx
import { useState } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'

type QuestionBlock = Extract<Block, { kind: 'question' }>

type AnswerMode = 'option' | 'freeform' | 'chat'

interface Props {
  block: QuestionBlock
  onAnswer: (questionId: string, selected: string[], mode?: AnswerMode) => void
  onChat?: (questionId: string) => void
  armedForChat?: boolean
}

function optionLabel(opt: Record<string, unknown>): string {
  return String(opt.label ?? opt)
}

const hatchBtn = cn(
  'rounded px-3 py-2 text-xs font-medium min-h-[44px]',
  'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
  'text-gray-700 dark:text-gray-200 hover:bg-blue-50 dark:hover:bg-blue-900/30',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)

export function QuestionCard({ block, onAnswer, onChat, armedForChat = false }: Props) {
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [writing, setWriting] = useState(false)
  const [draft, setDraft] = useState('')

  const containerClass = 'rounded border border-gray-200 dark:border-gray-700 p-3 my-1'
  const headerId = `q-header-${block.questionId}`

  const heading = (
    <>
      <div id={headerId} className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">
        {block.header}
      </div>
      <p className="mb-3 text-sm text-gray-700 dark:text-gray-300">{block.question}</p>
    </>
  )

  // Armed for chat: structured options collapse to a quiet banner; the user
  // types their reply in the main composer (routed back as the answer).
  if (armedForChat) {
    return (
      <div role="group" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <div className="flex items-center justify-between gap-2 rounded bg-blue-50 dark:bg-blue-900/30 px-3 py-2 text-sm text-blue-800 dark:text-blue-200">
          <span>💬 Replying in chat — type your message in the composer below.</span>
          <button type="button" onClick={() => onChat?.(block.questionId)} className={cn(hatchBtn, 'flex-shrink-0')}>
            Cancel
          </button>
        </div>
      </div>
    )
  }

  const escapeHatches = (
    <div className="mt-3 border-t border-gray-100 dark:border-gray-800 pt-3">
      {writing ? (
        <div className="space-y-2">
          <textarea
            aria-label="Write a response"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={2}
            placeholder="Type your own answer…"
            className={cn(
              'w-full resize-none rounded-md border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 text-sm',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 min-h-[44px]',
            )}
          />
          <button
            type="button"
            onClick={() => {
              const t = draft.trim()
              if (!t) return
              onAnswer(block.questionId, [t], 'freeform')
            }}
            className={cn(
              'rounded px-4 py-2 text-sm font-medium min-h-[44px]',
              'bg-blue-600 text-white hover:bg-blue-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            Send response
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setWriting(true)} className={hatchBtn}>
            ✍️ Write a response
          </button>
          <button type="button" onClick={() => onChat?.(block.questionId)} className={hatchBtn}>
            💬 Chat about this
          </button>
        </div>
      )}
    </div>
  )

  if (!block.multiSelect) {
    return (
      <div role="radiogroup" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <div className="space-y-1">
          {block.options.map((opt, i) => {
            const label = optionLabel(opt)
            return (
              <button
                key={i}
                type="button"
                role="radio"
                aria-checked={false}
                onClick={() => onAnswer(block.questionId, [label])}
                className={cn(
                  'w-full text-left rounded px-3 py-2 text-sm',
                  'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                  'text-gray-800 dark:text-gray-200',
                  'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                  'min-h-[44px]',
                )}
              >
                {label}
              </button>
            )
          })}
        </div>
        {escapeHatches}
      </div>
    )
  }

  return (
    <div role="group" aria-labelledby={headerId} className={containerClass}>
      {heading}
      <div className="space-y-1 mb-3">
        {block.options.map((opt, i) => {
          const label = optionLabel(opt)
          return (
            <label
              key={i}
              className={cn(
                'flex items-center gap-2 rounded px-3 py-2 text-sm cursor-pointer',
                'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                'text-gray-800 dark:text-gray-200',
                'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                'min-h-[44px]',
              )}
            >
              <input
                type="checkbox"
                checked={checked.has(label)}
                onChange={(e) => {
                  setChecked((prev) => {
                    const next = new Set(prev)
                    if (e.target.checked) next.add(label)
                    else next.delete(label)
                    return next
                  })
                }}
                className="h-4 w-4 rounded border-gray-300"
              />
              <span>{label}</span>
            </label>
          )
        })}
      </div>
      <button
        type="button"
        onClick={() => onAnswer(block.questionId, Array.from(checked))}
        className={cn(
          'rounded px-4 py-2 text-sm font-medium',
          'bg-blue-600 text-white hover:bg-blue-700',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'min-h-[44px]',
        )}
      >
        Submit
      </button>
      {escapeHatches}
    </div>
  )
}
```

- [ ] **Step 4: Run the QuestionCard tests to verify pass.**

Run: `cd web && npx vitest run src/components/session/__tests__/QuestionCard.test.tsx`
Expected: PASS — new escape-hatch tests and all six pre-existing tests (radios/checkboxes/submit untouched).

- [ ] **Step 5: Commit.**

```bash
git add web/src/components/session/QuestionCard.tsx web/src/components/session/__tests__/QuestionCard.test.tsx
git commit -m "feat(QuestionCard): add free-form and chat escape hatches

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — thread props through `Turn` and `TranscriptView`

**Files:**
- Modify: `web/src/components/session/Turn.tsx:15-52`
- Modify: `web/src/components/session/TranscriptView.tsx:5-54`

**Interfaces:**
- Consumes: `QuestionCard` props `onChat`, `armedForChat`, and `onAnswer` with optional `mode` (Task 5).
- Produces: both components accept `onAnswer: (questionId, selected, mode?: 'option'|'freeform'|'chat') => void`, `onChat: (questionId: string) => void`, and `armedQuestionId: string | null`, and pass `armedForChat={block.questionId === armedQuestionId}` to `QuestionCard`. (No new test file — covered by type-check plus the Task 7 SessionView test.)

- [ ] **Step 1: Update `Turn.tsx`.**

Replace the `Props` interface and the `question` case in `web/src/components/session/Turn.tsx`:

```tsx
interface Props {
  turn: TurnType
  permissionMode: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  onChat: (questionId: string) => void
  armedQuestionId: string | null
}

export const Turn = memo(function Turn({ turn, permissionMode, onDecision, onAnswer, onChat, armedQuestionId }: Props) {
```

And the question case (line ~44):

```tsx
          case 'question':
            return (
              <QuestionCard
                key={block.id}
                block={block}
                onAnswer={onAnswer}
                onChat={onChat}
                armedForChat={block.questionId === armedQuestionId}
              />
            )
```

- [ ] **Step 2: Update `TranscriptView.tsx`.**

Replace the `Props` interface and the `<Turn>` render in `web/src/components/session/TranscriptView.tsx`:

```tsx
interface Props {
  turns: TurnType[]
  permissionMode: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  onChat: (questionId: string) => void
  armedQuestionId: string | null
}

export function TranscriptView({ turns, permissionMode, onDecision, onAnswer, onChat, armedQuestionId }: Props) {
```

And the mapped `<Turn>` (line ~42):

```tsx
        <Turn
          key={turn.id}
          turn={turn}
          permissionMode={permissionMode}
          onDecision={onDecision}
          onAnswer={onAnswer}
          onChat={onChat}
          armedQuestionId={armedQuestionId}
        />
```

- [ ] **Step 3: Type-check (SessionView still passes the old props — expected to fail here).**

Run: `cd web && npx tsc --noEmit`
Expected: FAIL — `SessionView.tsx` does not yet pass `onChat`/`armedQuestionId` to `TranscriptView`. This is resolved in Task 7. (Do not commit a red type-check on its own; this task's commit is folded into Task 7's verification. Proceed directly to Task 7.)

---

## Task 7: Frontend — `SessionView` wiring + `Composer` armed placeholder

**Files:**
- Modify: `web/src/components/session/SessionView.tsx` (handlers, state, prop wiring)
- Modify: `web/src/components/session/Composer.tsx:1-14, 86-103`
- Test: `web/src/components/session/__tests__/SessionView.test.tsx`

**Interfaces:**
- Consumes: `TranscriptView` props `onChat`, `armedQuestionId`, `onAnswer(…, mode?)` (Task 6); `answerQuestion(sessionId, questionId, selected, mode)` (Task 4); `Composer`'s `chatArmed` prop.
- Produces: `SessionView` holds `armedQuestionId: string | null`. "Chat about this" toggles it. While armed, the next composer send calls `answerQuestion(sessionId, armedQuestionId, [text], 'chat')` (and `_resolveQuestion`) instead of `prompt(...)`. Free-form answers call `answerQuestion(sessionId, qid, selected, 'freeform')`. Arming clears on interrupt and when the armed question is no longer present.

- [ ] **Step 1: Write failing integration tests.**

Add to `web/src/components/session/__tests__/SessionView.test.tsx` (reuse the existing `feedEvent`/render helpers in that file; a `question` event has shape `{ type: 'question', questionId, turnId, header, question, options, multiSelect }`). Add a `describe` block:

```ts
describe('SessionView question escape hatches', () => {
  beforeEach(() => {
    capturedHandlers = {}
    vi.clearAllMocks()
  })

  function renderAndAskQuestion() {
    const utils = renderSessionView() // existing helper that renders <SessionView sessionId="s-1" .../>
    const emit = (e: SessionEvent) => {
      for (const cb of capturedHandlers['sessionEvent'] ?? []) act(() => cb(e))
    }
    emit({ type: 'turnStart', turnId: 't1' } as SessionEvent)
    emit({
      type: 'question', questionId: 'q-9', turnId: 't1', header: 'H',
      question: 'Pick one', options: [{ label: 'A' }], multiSelect: false,
    } as SessionEvent)
    return utils
  }

  it('chat: arming routes the next composer message to answerQuestion(mode=chat)', async () => {
    const user = userEvent.setup()
    renderAndAskQuestion()
    await user.click(screen.getByRole('button', { name: /chat about this/i }))
    // Card collapsed to banner.
    expect(screen.getByText(/replying in chat/i)).toBeInTheDocument()
    // Type in the composer and send.
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'lets talk instead')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    expect(mockAnswerQuestion).toHaveBeenCalledWith('s-1', 'q-9', ['lets talk instead'], 'chat')
    expect(mockPrompt).not.toHaveBeenCalled()
  })

  it('free-form: inline response calls answerQuestion(mode=freeform)', async () => {
    const user = userEvent.setup()
    renderAndAskQuestion()
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'my words')
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(mockAnswerQuestion).toHaveBeenCalledWith('s-1', 'q-9', ['my words'], 'freeform')
  })

  it('normal prompt is unaffected when no question is armed', async () => {
    const user = userEvent.setup()
    renderAndAskQuestion()
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'normal message')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    expect(mockPrompt).toHaveBeenCalledWith('s-1', 'normal message')
    expect(mockAnswerQuestion).not.toHaveBeenCalled()
  })
})
```

> Implementation note: if the test file has no reusable `renderSessionView()` helper, add a tiny one mirroring the existing render setup (wrap `<SessionView sessionId="s-1" onBack={vi.fn()} />` in `QueryClientProvider` with a fresh `QueryClient`), and confirm `sessionId` is `'s-1'` so the `toHaveBeenCalledWith` first arg matches. Use the actual `sessionId` the existing tests use if different.

- [ ] **Step 2: Run to verify failure.**

Run: `cd web && npx vitest run src/components/session/__tests__/SessionView.test.tsx`
Expected: FAIL — `onChat`/armed wiring absent; chat routing not implemented.

- [ ] **Step 3: Add the `chatArmed` prop to `Composer`.**

In `web/src/components/session/Composer.tsx`, add to `Props`:

```tsx
interface Props {
  status: { state: string; count: number }
  mode: string
  onPrompt: (text: string) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
  queued?: string[]
  onCancelQueued?: (index: number) => void
  chatArmed?: boolean
}
```

Destructure it and use it for the placeholder. Replace the function signature line (14):

```tsx
export function Composer({ status, mode, onPrompt, onInterrupt, onSetMode, queued, onCancelQueued, chatArmed }: Props) {
```

And the textarea `placeholder` (line ~93):

```tsx
          placeholder={chatArmed ? 'Reply in chat…' : 'Send a message…'}
```

- [ ] **Step 4: Wire `SessionView` — state, handlers, prop passthrough.**

In `web/src/components/session/SessionView.tsx`:

(a) Add state after `rotations` (line ~68):

```tsx
  const [armedQuestionId, setArmedQuestionId] = useState<string | null>(null)
```

(b) Replace `handleAnswer` (lines 132-138):

```tsx
  // Answer handler for question cards (option clicks and inline free-form)
  const handleAnswer = useCallback(
    (questionId: string, selected: string[], mode: 'option' | 'freeform' | 'chat' = 'option') => {
      answerQuestion(sessionId, questionId, selected, mode)
      dispatch({ type: '_resolveQuestion', questionId })
      setArmedQuestionId((cur) => (cur === questionId ? null : cur))
    },
    [sessionId, answerQuestion],
  )

  // Toggle "armed for chat" for a question: the next composer send becomes its answer.
  const handleChat = useCallback(
    (questionId: string) => {
      setArmedQuestionId((cur) => (cur === questionId ? null : questionId))
    },
    [],
  )
```

(c) Replace `handlePrompt` (lines 141-144) so it routes to the armed question:

```tsx
  // Composer handlers — when a question is armed for chat, the next message
  // answers that question (mode='chat') and unblocks the turn; otherwise it's a
  // normal prompt.
  const handlePrompt = useCallback(
    (text: string) => {
      if (armedQuestionId) {
        answerQuestion(sessionId, armedQuestionId, [text], 'chat')
        dispatch({ type: '_resolveQuestion', questionId: armedQuestionId })
        setArmedQuestionId(null)
        return
      }
      prompt(sessionId, text)
    },
    [sessionId, prompt, answerQuestion, armedQuestionId],
  )
```

(d) Clear arming on interrupt — replace `handleInterrupt` (lines 145-148):

```tsx
  const handleInterrupt = useCallback(
    () => {
      setArmedQuestionId(null)
      sendInterrupt(sessionId)
    },
    [sessionId, sendInterrupt],
  )
```

(e) Add a defensive cleanup effect (place after the `hasOpenAction` declaration, ~line 172) so a stale arm clears if the question disappears:

```tsx
  // If the armed question is no longer present (resolved/replaced), disarm.
  useEffect(() => {
    if (!armedQuestionId) return
    const stillOpen = state.turns.some((t) =>
      t.blocks.some((b) => b.kind === 'question' && b.questionId === armedQuestionId),
    )
    if (!stillOpen) setArmedQuestionId(null)
  }, [state.turns, armedQuestionId])
```

(f) Pass new props to `TranscriptView` (lines 283-288):

```tsx
      <TranscriptView
        turns={state.turns}
        permissionMode={mode}
        onDecision={handleDecision}
        onAnswer={handleAnswer}
        onChat={handleChat}
        armedQuestionId={armedQuestionId}
      />
```

(g) Pass `chatArmed` to `Composer` (lines 328-336):

```tsx
        <Composer
          status={state.status}
          mode={mode}
          onPrompt={handlePrompt}
          onInterrupt={handleInterrupt}
          onSetMode={handleSetMode}
          queued={state.queued}
          onCancelQueued={(i) => cancelQueued(sessionId, i)}
          chatArmed={armedQuestionId !== null}
        />
```

- [ ] **Step 5: Run SessionView tests to verify pass.**

Run: `cd web && npx vitest run src/components/session/__tests__/SessionView.test.tsx`
Expected: PASS — the three new tests plus all existing SessionView tests.

- [ ] **Step 6: Full frontend verification (type-check, lint, all tests).**

Run: `cd web && npx tsc --noEmit && npx eslint src && npx vitest run`
Expected: PASS — no type errors (Task 6's prop additions now satisfied), no lint errors, all tests green.

- [ ] **Step 7: Commit.**

```bash
git add web/src/components/session/SessionView.tsx web/src/components/session/Composer.tsx web/src/components/session/Turn.tsx web/src/components/session/TranscriptView.tsx web/src/components/session/__tests__/SessionView.test.tsx
git commit -m "feat(session): route free-form and chat answers from the UI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before PR)

- [ ] Backend: `python -m pytest server/tests/ -q` (via the main-checkout venv; if it 401s on auth tests, use `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests/ -q`). Expected: all pass.
- [ ] Frontend: `cd web && npx tsc --noEmit && npx eslint src && npx vitest run`. Expected: all pass.
- [ ] Manual smoke (optional, via the `run` skill): trigger an `AskUserQuestion` in a live session, confirm "Write a response" sends inline and "Chat about this" routes the next composer message and unblocks the turn.
- [ ] Open a PR per the completion workflow (`gh pr create`, Claude Code trailer), merge once mergeable.

---

## Self-Review

**Spec coverage:**
- "Always offer write-a-response + chat-about-this on AskUserQuestion cards" → Tasks 5 (UI), 6 (threading), 7 (wiring). ✓
- "Both paths resolve the pending question future / unblock the turn" → Task 7 free-form (`answerQuestion` mode=freeform) and chat (`answerQuestion` mode=chat) both call `resolve` (Tasks 1-2). ✓
- "Intent-appropriate framing to the model" → Task 2 `_format_answer_message`/`_format_answer_line`. ✓
- "Preserve fast tap-to-pick, a11y, 44px" → Task 5 keeps radios/checkboxes/Submit and `min-h-[44px]`, roles unchanged. ✓
- "Backward compatible, mode optional default option" → defaults at registry (Task 1), driver/orchestrator (Task 2), ws (Task 3), client (Task 4). ✓
- "Out of scope: permission/plan cards; no DB changes" → no `PermissionCard`/`ExitPlanMode`/schema edits in any task. ✓
- "Arming clears on interrupt / detach" → Task 7 (d) interrupt + (e) defensive effect. ✓ (Detach also unmounts SessionView, discarding local state.)
- Tests: backend three-mode framing + dismissal (Task 2), registry mode (Task 1), ws validation (Task 3), QuestionCard escape hatches (Task 5), SessionView routing (Task 7). ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command states expected output. The one "implementation note" in Task 7 Step 1 gives concrete fallback code, not a vague instruction. ✓

**Type consistency:** `mode: 'option' | 'freeform' | 'chat'` used identically across `client.ts`, `QuestionCard`, `Turn`, `TranscriptView`, `SessionView`. `onAnswer(questionId, selected, mode?)` and `onChat(questionId)` signatures match across `QuestionCard` → `Turn` → `TranscriptView` → `SessionView`. Backend `answer_question(question_id, selected, identity, mode="option")` consistent across `driver.py` and `orchestrator.py`; registry future result `(selected, mode) | None` consistent between `resolve`/`deny_all` (producers) and `_handle_ask_user_question` (consumer). ✓
