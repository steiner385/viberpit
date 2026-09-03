/**
 * Task 7 — In-session keyboard shortcuts:
 *   Esc / ← / Ctrl+Z → detach back to the session list
 *   Shift+Tab        → nothing; D8 removed the binding (see the block below)
 *
 * ## Where these moved, and why they are all in ONE file now
 *
 * They were `SessionView.keyboard.test.tsx`, plus five cases in
 * `DesktopShell.test.tsx` that pinned the same `detachKeyYieldsToTextEntry`
 * contract against the desktop shell's own copy of the handler. Both hosts are
 * deleted; the handler is `components/session/useInSessionKeys.ts`, mounted by
 * `MainPaneSession` — one implementation, one suite. (Brief's Warning, option
 * (a): the handler was PORTED rather than dropped, so nothing regresses between
 * this PR and PR 4's shortcuts registry.)
 *
 * ## The harness renders the pane AND the composer
 *
 * That is not convenience: the guard's whole subject is "which text entry has
 * focus and does it hold a draft", and the composer is the one with the special
 * left-edge rule. In the app they are two regions inside ONE
 * `SessionRuntimeProvider` (`PageHost` renders the page and the dock inside it),
 * so the harness composes them the same way rather than approximating it.
 *
 * Mocks useSessionSocket with a stable object so the spies can be asserted on
 * after render.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MainPaneSession } from './MainPaneSession'
import { SessionComposer } from '@/components/session/SessionComposer'
import {
  SessionRuntimeProvider, useSessionRuntimeContext,
} from '@/components/session/SessionRuntimeProvider'

// ── Stable mock socket ────────────────────────────────────────────────────────

let capturedHandlers: Record<string, ((...args: unknown[]) => void)[]> = {}

// Returns true: these mocks stand in for an OPEN socket where the control frame is
// written successfully, so the optimistic mode change is applied (see #505 gating).
const mockSetPermissionMode = vi.fn().mockReturnValue(true)
const mockAttach = vi.fn()
const mockDetach = vi.fn()
const mockOff = vi.fn()
const mockOn = vi.fn().mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
  if (!capturedHandlers[event]) capturedHandlers[event] = []
  capturedHandlers[event].push(cb)
})

const stableMockSocket = {
  attach: mockAttach,
  detach: mockDetach,
  interrupt: vi.fn(),
  prompt: vi.fn(),
  sendInterrupt: vi.fn(),
  permissionResponse: vi.fn(),
  answerQuestion: vi.fn(),
  setPermissionMode: mockSetPermissionMode,
  on: mockOn,
  off: mockOff,
  connectionState: 'connected' as const,
  socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => stableMockSocket,
  }
})

// ── Wrapper ───────────────────────────────────────────────────────────────────

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

/** The dock's composer, reading the same runtime the pane does. */
function DockComposer() {
  const rt = useSessionRuntimeContext()
  return <SessionComposer rt={rt} />
}

/** `app.main` + `app.dock` for one open session — the shell's composition. */
function renderPane(onDetach: () => void) {
  return render(
    <SessionRuntimeProvider sessionId="s1">
      <MainPaneSession
        sessionId="s1"
        onClosed={vi.fn()}
        onDetach={onDetach}
        onOpenSession={vi.fn()}
      />
      <DockComposer />
    </SessionRuntimeProvider>,
    { wrapper },
  )
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('MainPaneSession keyboard shortcuts (Task 7)', () => {
  beforeEach(() => {
    capturedHandlers = {}
    mockSetPermissionMode.mockClear()
    mockAttach.mockClear()
    mockDetach.mockClear()
    mockOff.mockClear()
    mockOn.mockClear()
    mockOn.mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
      if (!capturedHandlers[event]) capturedHandlers[event] = []
      capturedHandlers[event].push(cb)
    })
  })

  it('Escape calls onBack when focus is NOT in the composer textarea', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    // Focus something other than the composer (the Back button)
    screen.getByRole('button', { name: /back to session list/i }).focus()
    await user.keyboard('{Escape}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('Escape calls onBack when the composer textarea is empty', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    // Click the textarea (leaving it empty) then press Escape
    await user.click(screen.getByRole('textbox', { name: /message/i }))
    await user.keyboard('{Escape}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('Escape does NOT call onBack when the composer textarea has text (mid-draft guard)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{Escape}')
    expect(onBack).not.toHaveBeenCalled()
  })

  // ── Shift+Tab: removed, not ported (D8) ───────────────────────────────────
  //
  // R9 successor map for the two cases that used to live here:
  //
  //   'Shift+Tab cycles the permission mode: interactive → auto'
  //   'Shift+Tab cycles through all modes in order'
  //
  // Both asserted a binding that sub-spec B **D8** removes, and that this
  // branch's cutover made actively harmful: the handler is a bare `window`
  // listener, so once `MainPaneSession` mounts inside `AppShell` — beside a
  // focusable rail, bar, dock and drawer — it swallowed `Shift+Tab` for the
  // whole document and walked a LIVE session's permission mode toward
  // `bypassPermissions` while the user was only trying to move focus backwards.
  // Neither case can have a "still cycles, but only when…" successor, because
  // the behaviour they pinned is gone.
  //
  // Their successor is the pair below, which pins the OPPOSITE contract at the
  // same seam (same harness, same window listener, same open session): the mode
  // does not move, and the event is left un-`preventDefault`ed so the browser's
  // own reverse-tab still happens. The mode selector in `SessionMeta` is the
  // supported path and keeps its own coverage
  // (`components/session/__tests__/SessionMeta.test.tsx:134-146`).

  it('Shift+Tab does not change the permission mode and does not swallow reverse-tab (D8)', () => {
    renderPane(vi.fn())
    // `fireEvent` returns false when a listener called preventDefault().
    const notPrevented = fireEvent.keyDown(document.body, { key: 'Tab', shiftKey: true })
    expect(notPrevented).toBe(true)
    expect(mockSetPermissionMode).not.toHaveBeenCalled()
  })

  it('Shift+Tab out of the composer is left to the browser (no mode change, no preventDefault)', async () => {
    const user = userEvent.setup()
    renderPane(vi.fn())
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    const notPrevented = fireEvent.keyDown(box, { key: 'Tab', shiftKey: true })
    expect(notPrevented).toBe(true)
    expect(mockSetPermissionMode).not.toHaveBeenCalled()
  })

  it('Escape does NOT call onBack when a permission card is pending (Fix 6)', async () => {
    const { act } = await import('@testing-library/react')
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    // Simulate a permissionRequest arriving so hasOpenAction becomes true
    await act(async () => {
      capturedHandlers['sessionEvent']?.forEach((cb) =>
        cb({
          type: 'turnStart',
          turnId: 't1',
        }),
      )
      capturedHandlers['sessionEvent']?.forEach((cb) =>
        cb({
          type: 'permissionRequest',
          permId: 'perm-1',
          turnId: 't1',
          tool: 'bash',
          toolKind: 'bash',
          input: { command: 'ls' },
          plan: false,
        }),
      )
    })
    // Escape should be a no-op while the card is pending
    await user.keyboard('{Escape}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('ArrowLeft on an empty composer calls onBack', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    await user.click(screen.getByRole('textbox', { name: /message/i }))
    await user.keyboard('{ArrowLeft}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('ArrowLeft does NOT call onBack when the composer has text and the caret is mid-prompt', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip') // caret left at the end (position 3)
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('ArrowLeft calls onBack when the composer has text but the caret is at the start', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = screen.getByRole('textbox', { name: /message/i }) as HTMLTextAreaElement
    await user.click(box)
    await user.type(box, 'wip')
    box.setSelectionRange(0, 0) // caret at the very start of the prompt
    fireEvent.keyDown(box, { key: 'ArrowLeft' })
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('ArrowLeft does NOT call onBack when text is selected from the start (collapsing the selection)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = screen.getByRole('textbox', { name: /message/i }) as HTMLTextAreaElement
    await user.click(box)
    await user.type(box, 'wip')
    box.setSelectionRange(0, 3) // whole prompt selected; ← should collapse, not navigate
    fireEvent.keyDown(box, { key: 'ArrowLeft' })
    expect(onBack).not.toHaveBeenCalled()
  })

  it('ArrowLeft does NOT call onBack while a permission card is pending', async () => {
    const { act } = await import('@testing-library/react')
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    await act(async () => {
      capturedHandlers['sessionEvent']?.forEach((cb) => cb({ type: 'turnStart', turnId: 't1' }))
      capturedHandlers['sessionEvent']?.forEach((cb) =>
        cb({
          type: 'permissionRequest',
          permId: 'perm-1',
          turnId: 't1',
          tool: 'bash',
          toolKind: 'bash',
          input: { command: 'ls' },
          plan: false,
        }),
      )
    })
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('Ctrl+Z force-detaches even while a permission card is pending', async () => {
    const { act } = await import('@testing-library/react')
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    await act(async () => {
      capturedHandlers['sessionEvent']?.forEach((cb) => cb({ type: 'turnStart', turnId: 't1' }))
      capturedHandlers['sessionEvent']?.forEach((cb) =>
        cb({
          type: 'permissionRequest',
          permId: 'perm-1',
          turnId: 't1',
          tool: 'bash',
          toolKind: 'bash',
          input: { command: 'ls' },
          plan: false,
        }),
      )
    })
    await user.keyboard('{Control>}z{/Control}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('Ctrl+Z does NOT detach when the composer has text (undo preserved)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{Control>}z{/Control}')
    expect(onBack).not.toHaveBeenCalled()
  })

  // ── ported from DesktopShell.test.tsx ──────────────────────────────────────
  //
  // Five cases there pinned the SAME `detachKeyYieldsToTextEntry` contract
  // against the desktop shell's own copy of the handler. Four of them are about
  // the question card's "Write a response" box — a text entry that is stricter
  // than the composer: it holds ←, Esc and Ctrl+Z whenever it has ANY text,
  // because it unmounts on detach and the draft would be lost. The fifth (the
  // composer's left-edge ← rule) is already the "caret is at the start" case
  // above. Their assertion was "the new-session prompt is/is not back on
  // screen", which was the desktop shell's way of observing a detach; here the
  // detach itself is observable.

  /**
   * Deliver a LIVE question and open its custom-answer box.
   *
   * `replayComplete` first: the reducer starts in replaying mode and a question
   * that arrives during replay is `live: false`, so the pinned panel — the only
   * thing that renders the escape hatch — would never appear.
   */
  async function openQuestionDraft(user: ReturnType<typeof userEvent.setup>) {
    const { act } = await import('@testing-library/react')
    await act(async () => {
      const emit = (frame: Record<string, unknown>) =>
        capturedHandlers['sessionEvent']?.forEach((cb) => cb(frame))
      emit({ type: 'replayComplete' })
      emit({ type: 'turnStart', turnId: 't1' })
      emit({
        type: 'question', questionId: 'q1', turnId: 't1',
        header: 'Pick one', question: 'Which approach?',
        options: [{ label: 'Option A' }, { label: 'Option B' }], multiSelect: false,
      })
    })
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    return screen.getByRole('textbox', { name: /write a response/i }) as HTMLTextAreaElement
  }

  it('ArrowLeft while typing a custom question answer does NOT detach (draft preserved)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = await openQuestionDraft(user)
    await user.type(box, 'my custom answer')
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()
    expect(box).toHaveValue('my custom answer')
  })

  it('ArrowLeft at the START of a custom question answer still does NOT detach', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = await openQuestionDraft(user)
    await user.type(box, 'my custom answer')
    box.setSelectionRange(0, 0) // unlike the composer, a non-empty draft never detaches
    fireEvent.keyDown(box, { key: 'ArrowLeft' })
    expect(onBack).not.toHaveBeenCalled()
    expect(box).toHaveValue('my custom answer')
  })

  it('Escape while typing a custom question answer does NOT detach (draft preserved)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = await openQuestionDraft(user)
    await user.type(box, 'my custom answer')
    await user.keyboard('{Escape}')
    expect(onBack).not.toHaveBeenCalled()
    expect(box).toHaveValue('my custom answer')
  })

  /**
   * BEHAVIOUR CHANGE, recorded rather than hidden.
   *
   * Against `DesktopShell` this case DETACHED: an empty answer box has no draft
   * to protect, so `detachKeyYieldsToTextEntry` let ← through. It no longer
   * does, because the two handlers were unified onto the STRICTER of the pair —
   * `SessionView`'s, which also refuses to detach while a permission or question
   * card is unanswered (`hasOpenAction`). The desktop copy simply never had that
   * guard.
   *
   * The union is deliberate: the guard is the reason ← over an unanswered card
   * does not silently walk away from it, and it is the behaviour the phone (the
   * only place a question card is unavoidable) has always had. Ctrl+Z stays the
   * force-detach escape hatch, so the user is never trapped — asserted below, in
   * the same case, because "it no longer detaches" is only acceptable while
   * something still does.
   */
  it('ArrowLeft in an EMPTY custom-answer box holds while the question is unanswered; Ctrl+Z still detaches', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    renderPane(onBack)
    const box = await openQuestionDraft(user)
    box.focus()
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()

    await user.keyboard('{Control>}z{/Control}')
    expect(onBack).toHaveBeenCalledOnce()
  })
})
