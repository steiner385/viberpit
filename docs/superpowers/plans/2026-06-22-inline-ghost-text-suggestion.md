# Inline Ghost-Text Suggestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the next-prompt suggestion *chips* with a single faded inline ghost-text suggestion accepted via Tab/→ (fills the box, no auto-send), and make suggestions actually generate for resumed/idle sessions with observable logging.

**Architecture:** Reuse the existing generation pipeline (`SuggestionService` → `ev.Suggestions` → reducer `latestSuggestions` → Composer prop) unchanged. Add a pure transcript-tail extractor and a driver method so a session resumed into idle generates one suggestion from its replayed transcript; wire it into the WS attach path. Swap the Composer's chip row for a ghost-text overlay over the textarea.

**Tech Stack:** Python (FastAPI/asyncio, pytest) backend · React + TypeScript + Tailwind (vitest/RTL) frontend.

## Global Constraints

- TDD: write the failing test first, watch it fail, implement, watch it pass, commit. One logical change per commit.
- Server tests: `pytest server/tests/`. Web tests (run in `web/`): `vitest run`, `tsc --noEmit`, `eslint`.
- Fresh worktree has no built env: run server tests via the main checkout's prebuilt interpreter — `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest …` (or the main checkout's `.venv`) — and symlink `web/node_modules` from the main checkout before running web tests.
- API/auth not touched here; no DB schema change.
- Frontend interactive controls keep the existing `min-h-[44px]` tap-target convention; ghost overlay is non-interactive (`pointer-events-none`).
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Suggestion generation model/prompt/isolation are unchanged — we only add a new *trigger* and change *presentation*.

---

### Task 1: Transcript-tail context extractor

**Files:**
- Modify: `server/sessions/suggestions.py`
- Test: `server/tests/test_suggestions.py`

**Interfaces:**
- Consumes: `server.sessions.events.UserMessage` (`turn_id: str; text: str`), `server.sessions.events.TextDelta` (`turn_id: str; text: str`) — the replayed event objects returned by `server.sessions.replay.replay_events`.
- Produces: `last_turn_context(events: list) -> tuple[str, str]` returning `(user_text, assistant_text)`. `user_text` is the most-recent `UserMessage.text`; `assistant_text` is the concatenation of every `TextDelta.text` after that message. Returns `("", "")` when no `UserMessage` is present.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_suggestions.py`:

```python
def test_last_turn_context_extracts_latest_user_and_following_assistant():
    from server.sessions import events as ev
    from server.sessions.suggestions import last_turn_context
    evs = [
        ev.UserMessage("t1", "first question"),
        ev.TextDelta("t1", "old answer"),
        ev.UserMessage("t2", "do the thing"),
        ev.TextDelta("t2", "Sure, "),
        ev.TextDelta("t2", "done."),
    ]
    assert last_turn_context(evs) == ("do the thing", "Sure, done.")


def test_last_turn_context_empty_when_no_user_message():
    from server.sessions.suggestions import last_turn_context
    assert last_turn_context([]) == ("", "")
    from server.sessions import events as ev
    assert last_turn_context([ev.TextDelta("t1", "orphan")]) == ("", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5b995942 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_suggestions.py::test_last_turn_context_extracts_latest_user_and_following_assistant -v`
Expected: FAIL with `ImportError: cannot import name 'last_turn_context'`.

- [ ] **Step 3: Write minimal implementation**

Add to `server/sessions/suggestions.py` (top-level function, near `parse_suggestions`):

```python
def last_turn_context(events: list) -> tuple[str, str]:
    """Extract (user_text, assistant_text) for the latest turn from replayed events.

    user_text is the most-recent UserMessage's text; assistant_text concatenates the
    TextDelta texts that follow it. Returns ("", "") when there's no user message.
    Used to seed suggestion generation for a session resumed into an idle state,
    whose turn context is not in the driver's memory.
    """
    from . import events as ev
    last_user = -1
    for i, e in enumerate(events):
        if isinstance(e, ev.UserMessage):
            last_user = i
    if last_user == -1:
        return "", ""
    user_text = events[last_user].text or ""
    parts = [e.text for e in events[last_user + 1:] if isinstance(e, ev.TextDelta)]
    return user_text, "".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5b995942 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_suggestions.py -v`
Expected: PASS (all, including the two new tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/suggestions.py server/tests/test_suggestions.py
git commit -m "feat(suggestions): last_turn_context extractor for resumed-session seeding

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Driver idle-seed trigger + generation logging

**Files:**
- Modify: `server/sessions/driver.py` (`__init__` ~line 63-67; `_run_suggestions` ~line 386-389; add `seed_idle_suggestions`)
- Test: `server/tests/test_driver_suggestions.py`

**Interfaces:**
- Consumes: existing `self._suggester`, `self._maybe_start_suggestions()`, `self._turn_active`, `self._last_user_text`, `self._last_assistant_parts`, `self._last_turn_id`.
- Produces:
  - `SessionDriver.seed_idle_suggestions(user_text: str, assistant_text: str) -> None` — generates suggestions once for a resumed/idle driver using transcript-derived context. No-op if there's no suggester, if a live turn has produced context, or if already seeded.
  - `_run_suggestions` now logs an INFO line `suggestions session=<id> turn=<turn> n=<count>` for every generation attempt (count may be 0).

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_driver_suggestions.py` (reuses the existing `_factory` and `_StubSuggester` helpers in that file):

```python
def test_seed_idle_suggestions_emits_once(caplog):
    sug = _StubSuggester(["Resume hint"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        d.seed_idle_suggestions("do the thing", "earlier answer")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
                if isinstance(e, Suggestions):
                    return
        await asyncio.wait_for(asyncio.create_task(consume()), timeout=1.0)
        d.seed_idle_suggestions("do the thing", "earlier answer")  # 2nd attach: no-op
        await asyncio.sleep(0.05)
        await d.aclose()
        return got
    with caplog.at_level("INFO"):
        got = asyncio.run(run())
    sug_ev = next(e for e in got if isinstance(e, Suggestions))
    assert sug_ev.items == ["Resume hint"]
    assert sug.calls == 1
    assert "suggestions session=s1" in caplog.text


def test_seed_idle_suggestions_noop_without_context():
    sug = _StubSuggester(["x"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        d.seed_idle_suggestions("   ", "")  # blank → no generation
        await asyncio.sleep(0.05)
        await d.aclose()
        return d._suggest_task
    task = asyncio.run(run())
    assert task is None
    assert sug.calls == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5b995942 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_driver_suggestions.py::test_seed_idle_suggestions_emits_once -v`
Expected: FAIL with `AttributeError: 'SessionDriver' object has no attribute 'seed_idle_suggestions'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/driver.py` `__init__`, after `self._last_turn_id: str = ""` (line ~67) add:

```python
        self._idle_seeded: bool = False
```

Replace `_run_suggestions` (lines ~386-389) with:

```python
    async def _run_suggestions(self, context: str, turn_id: str) -> None:
        items = await self._suggester.generate(self.cwd, self.config_dir, context)
        log.info("suggestions session=%s turn=%s n=%d", self.session_id, turn_id, len(items))
        if items:
            await self._out.put(ev.Suggestions(self.session_id, turn_id, items))
```

Add the new method directly below `_run_suggestions`:

```python
    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None:
        """Generate suggestions for a session resumed into an idle state.

        Unlike _maybe_start_suggestions (fired at turn completion from in-memory
        context), this seeds context from the replayed transcript. Runs at most once
        per driver and only while no live turn has produced context, so repeated WS
        attaches don't regenerate; a later real turn still refreshes via drain().
        Best-effort and gated (no permission/question pending) inside
        _maybe_start_suggestions.
        """
        if self._suggester is None or self._idle_seeded:
            return
        if self._turn_active or self._last_assistant_parts:
            return
        if not (user_text.strip() or assistant_text.strip()):
            return
        self._idle_seeded = True
        self._last_user_text = user_text
        self._last_assistant_parts = [assistant_text]
        if not self._last_turn_id:
            self._last_turn_id = "resumed"
        self._maybe_start_suggestions()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5b995942 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_driver_suggestions.py -v`
Expected: PASS (existing tests + the two new ones).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_suggestions.py
git commit -m "feat(suggestions): seed_idle_suggestions for resumed sessions + generation log

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire idle-seed into WS attach

**Files:**
- Modify: `server/sessions/suggestions.py` (add `kick_idle_suggestions`)
- Modify: `server/bridge/ws.py` (`_handle_attach`, after the replay block ~line 198)
- Test: `server/tests/test_suggestions.py`

**Interfaces:**
- Consumes: `last_turn_context` (Task 1); `SessionDriver.seed_idle_suggestions` (Task 2).
- Produces: `kick_idle_suggestions(driver, events: list) -> None` — extracts the latest-turn context from `events` and calls `driver.seed_idle_suggestions(...)`. Pure glue; safe to call with `events == []` (driver no-ops on blank context).

This keeps the WS handler a one-liner and makes the wiring unit-testable without disk/socket setup (FakeSDKClient does not write a real transcript, so a full WS integration test can't exercise the transcript tail).

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_suggestions.py`:

```python
def test_kick_idle_suggestions_passes_extracted_context():
    from server.sessions import events as ev
    from server.sessions.suggestions import kick_idle_suggestions

    class _SpyDriver:
        def __init__(self): self.seeded = None
        def seed_idle_suggestions(self, user_text, assistant_text):
            self.seeded = (user_text, assistant_text)

    d = _SpyDriver()
    kick_idle_suggestions(d, [ev.UserMessage("t1", "ship it"), ev.TextDelta("t1", "On it.")])
    assert d.seeded == ("ship it", "On it.")


def test_kick_idle_suggestions_blank_events_still_calls_driver():
    from server.sessions.suggestions import kick_idle_suggestions

    class _SpyDriver:
        def __init__(self): self.seeded = None
        def seed_idle_suggestions(self, user_text, assistant_text):
            self.seeded = (user_text, assistant_text)

    d = _SpyDriver()
    kick_idle_suggestions(d, [])
    assert d.seeded == ("", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5b995942 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_suggestions.py::test_kick_idle_suggestions_passes_extracted_context -v`
Expected: FAIL with `ImportError: cannot import name 'kick_idle_suggestions'`.

- [ ] **Step 3: Write the implementation**

Add to `server/sessions/suggestions.py` (below `last_turn_context`):

```python
def kick_idle_suggestions(driver, events: list) -> None:
    """Trigger one idle-session suggestion generation from replayed transcript events.

    Glue for the WS attach path: extract the latest-turn context and hand it to the
    driver, which decides (once-only, gated) whether to generate.
    """
    user_text, assistant_text = last_turn_context(events)
    driver.seed_idle_suggestions(user_text, assistant_text)
```

In `server/bridge/ws.py` `_handle_attach`, the replay block already computes `events` (≈ line 183) and sends `replayComplete` (≈ line 198). Immediately **after** `await _send({"type": "replayComplete"})` add:

```python
            # A session resumed into an idle state has no in-memory turn context, so
            # the turn-completion suggestion trigger never fires. Seed one suggestion
            # from the replayed transcript tail (best-effort; driver gates + dedupes).
            try:
                from server.sessions.suggestions import kick_idle_suggestions
                kick_idle_suggestions(driver, events)
            except Exception:
                log.warning("ws.attach: idle suggestion seeding failed for %s", session_id, exc_info=True)
```

Note: `events` is in scope from the replay `try` block; if replay raised, it was assigned `[]` only inside that block — guard by initializing `events = []` before the replay `try` if a linter/`tsc`-equivalent flags possibly-unbound use. Verify `events` is defined on all paths; if not, add `events: list = []` just before the replay `try`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5b995942 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_suggestions.py server/tests/test_ws.py -v`
Expected: PASS (new glue tests + existing WS tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/suggestions.py server/bridge/ws.py server/tests/test_suggestions.py
git commit -m "feat(suggestions): seed idle suggestions from transcript on WS attach

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Composer ghost-text UX (replaces chips)

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Test: `web/src/components/session/Composer.test.tsx` (rewrite the suggestions describe block)

**Interfaces:**
- Consumes: existing `suggestions?: string[]` prop and `prefill(value)` helper; `statusTier(status.state) === 'busy'` → `isWorking`.
- Produces: when idle + empty + a suggestion exists, renders a `data-testid="ghost-suggestion"` faded overlay with a `⇥ Tab` hint; **Tab** (not Shift+Tab) or **ArrowRight** fills the box (no submit). The chip row (`data-testid="suggestion-chips"`) is removed.

- [ ] **Step 1: Rewrite the failing tests**

Replace the entire `describe('Composer suggestions', …)` block in `web/src/components/session/Composer.test.tsx` with:

```tsx
function setupStatus(suggestions: string[] | undefined, state = 'idle') {
  const onPrompt = vi.fn()
  render(
    <Composer
      status={{ state, count: 0 }}
      mode="default"
      onPrompt={onPrompt}
      onInterrupt={() => {}}
      onSetMode={() => {}}
      suggestions={suggestions}
    />,
  )
  return { onPrompt }
}

describe('Composer ghost-text suggestion', () => {
  it('shows the top suggestion as ghost text when idle and empty', () => {
    setupStatus(['Run the tests', 'Commit it'])
    const ghost = screen.getByTestId('ghost-suggestion')
    expect(ghost).toHaveTextContent('Run the tests')
    expect(ghost).toHaveTextContent('Tab')
  })

  it('hides ghost text while working', () => {
    setupStatus(['Run the tests'], 'in_progress')
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('hides ghost text when there are no suggestions', () => {
    setupStatus([])
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('Tab fills the box without sending and hides the ghost', () => {
    const { onPrompt } = setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.keyDown(textarea, { key: 'Tab' })
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })

  it('ArrowRight fills the box without sending', () => {
    const { onPrompt } = setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.keyDown(textarea, { key: 'ArrowRight' })
    expect(textarea.value).toBe('Run the tests')
    expect(onPrompt).not.toHaveBeenCalled()
  })

  it('Shift+Tab does not accept the suggestion', () => {
    setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.keyDown(textarea, { key: 'Tab', shiftKey: true })
    expect(textarea.value).toBe('')
  })

  it('hides ghost text once the user types', () => {
    setupStatus(['Run the tests'])
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'h' } })
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components/session/Composer.test.tsx`
Expected: FAIL (`ghost-suggestion` not found; old chip markup gone).

- [ ] **Step 3: Implement the ghost text**

In `web/src/components/session/Composer.tsx`:

a) After `const slashQuery = …` (line ~43) add the ghost computation:

```tsx
  const ghost =
    !isWorking && text === '' && suggestions && suggestions.length > 0
      ? suggestions[0]
      : null
```

b) In `handleKeyDown`, add the accept handling as the first branch inside the function (before the `pickingCommand` line ~105):

```tsx
    if (ghost && ((e.key === 'Tab' && !e.shiftKey) || e.key === 'ArrowRight')) {
      e.preventDefault()
      prefill(ghost)
      return
    }
```

c) Remove the entire chip block (lines ~158-178, the `{suggestions && suggestions.length > 0 && !isWorking && ( … )}` `data-testid="suggestion-chips"` `<div>`).

d) Wrap the `<textarea>` in a relative, flex-1 container so the overlay aligns over the text origin. Change the textarea class `flex-1` → `w-full`, suppress the placeholder while ghost is shown, and add the overlay. Replace the `<textarea … />` element with:

```tsx
        <div className="relative flex-1">
          <textarea
            ref={textareaRef}
            aria-label="Message"
            value={text}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={1}
            placeholder={ghost ? '' : (chatArmed ? 'Reply in chat…' : 'Send a message…')}
            className={cn(
              'w-full resize-none rounded-md border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
              'px-3 py-2 text-sm',
              'placeholder:text-gray-400 dark:placeholder:text-gray-500',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'min-h-[44px] overflow-y-auto',
            )}
            style={{ maxHeight: '192px' }}
          />
          {ghost && (
            <div
              data-testid="ghost-suggestion"
              aria-hidden="true"
              className={cn(
                'pointer-events-none absolute inset-0 flex items-center justify-between gap-2',
                'px-3 py-2 text-sm',
              )}
            >
              <span className="truncate text-gray-400 dark:text-gray-500">{ghost}</span>
              <span className={cn(
                'flex-shrink-0 rounded border px-1 text-[10px] leading-tight',
                'border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500',
              )}>
                ⇥ Tab
              </span>
            </div>
          )}
        </div>
```

(Keep the `<MicButton>`, file input, attach button, and send/queue/stop buttons exactly as they are — they remain siblings of this new `<div className="relative flex-1">` inside the existing `<div className="relative flex items-end gap-2">`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/components/session/Composer.test.tsx && npx tsc --noEmit && npx eslint src/components/session/Composer.tsx`
Expected: PASS (vitest green; no type or lint errors).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/Composer.test.tsx
git commit -m "feat(web): inline ghost-text next-prompt suggestion (replaces chips)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before PR)

- [ ] Server suite: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/ -q` — all green.
- [ ] Web (in `web/`, node_modules symlinked from main checkout): `npx vitest run && npx tsc --noEmit && npx eslint src` — all green.
- [ ] Manual: on the live instance after deploy, open a previously-idle session; confirm a single faded suggestion with a `⇥ Tab` hint appears, Tab/→ fills the box, typing dismisses it. Confirm `journalctl -u ccswitch.service | grep "suggestions session="` shows generation lines (count > 0 verifies the Haiku generator actually returns parseable output — the previously-unobservable risk).
- [ ] Open PR off `feat/ghost-text-suggestion`; merge per the CLAUDE.md completion workflow.

## Self-review notes

- **Spec coverage:** ghost text + Tab/→ fill-no-send + `⇥ Tab` hint (Task 4); reuse of `latestSuggestions[0]` and chip removal (Task 4); resumed/idle trigger (Tasks 1–3); observability log (Task 2); tests for every behavior (all tasks). ✓
- **Single-suggestion display:** generation still returns up to 4; the UI consumes `suggestions[0]`. Matches the "minimize blast radius" decision in the spec. ✓
- **Type/name consistency:** `last_turn_context` → `kick_idle_suggestions` → `seed_idle_suggestions` chain uses consistent signatures across Tasks 1–3; `ghost`, `prefill`, `isWorking`, `data-testid="ghost-suggestion"` consistent in Task 4. ✓
