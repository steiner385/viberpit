/**
 * Task 7 — In-session keyboard shortcuts:
 *   Esc → back to session list
 *   Shift+Tab → cycle permission mode via setPermissionMode
 *
 * Mocks useSessionSocket the same way SessionView.test.tsx does — importOriginal +
 * override just the hook. The stable mock object lets us assert on spies after render.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionView } from '../SessionView'

// ── Stable mock socket ────────────────────────────────────────────────────────

let capturedHandlers: Record<string, ((...args: unknown[]) => void)[]> = {}

const mockSetPermissionMode = vi.fn()
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

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('SessionView keyboard shortcuts (Task 7)', () => {
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
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
    // Focus something other than the composer (the Back button)
    screen.getByRole('button', { name: /back to session list/i }).focus()
    await user.keyboard('{Escape}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('Escape calls onBack when the composer textarea is empty', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
    // Click the textarea (leaving it empty) then press Escape
    await user.click(screen.getByRole('textbox', { name: /message/i }))
    await user.keyboard('{Escape}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('Escape does NOT call onBack when the composer textarea has text (mid-draft guard)', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{Escape}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('Shift+Tab cycles the permission mode: interactive → auto', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    // Initial mode is 'interactive'; cycling once should call setPermissionMode with 'auto'
    expect(mockSetPermissionMode).toHaveBeenCalledWith('s1', 'auto')
  })

  it('Shift+Tab cycles through all modes in order', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} onDone={vi.fn()} />, { wrapper })
    // interactive → auto → allowlist → full-auto → interactive
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(mockSetPermissionMode).toHaveBeenLastCalledWith('s1', 'auto')
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(mockSetPermissionMode).toHaveBeenLastCalledWith('s1', 'allowlist')
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(mockSetPermissionMode).toHaveBeenLastCalledWith('s1', 'full-auto')
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(mockSetPermissionMode).toHaveBeenLastCalledWith('s1', 'interactive')
  })

  it('Escape does NOT call onBack when a permission card is pending (Fix 6)', async () => {
    const { act } = await import('@testing-library/react')
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
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
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /message/i }))
    await user.keyboard('{ArrowLeft}')
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('ArrowLeft does NOT call onBack when the composer has text and the caret is mid-prompt', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip') // caret left at the end (position 3)
    await user.keyboard('{ArrowLeft}')
    expect(onBack).not.toHaveBeenCalled()
  })

  it('ArrowLeft calls onBack when the composer has text but the caret is at the start', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
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
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
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
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
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
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
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
    render(<SessionView sessionId="s1" onBack={onBack} onDone={vi.fn()} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /message/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{Control>}z{/Control}')
    expect(onBack).not.toHaveBeenCalled()
  })
})
