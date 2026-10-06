import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionComposer } from './SessionComposer'
import type { MentionSource } from './MentionMenu'
import type { SessionRuntime } from '@/session/useSessionRuntime'

// SessionComposer now owns the on-demand suggestion mutation (useGenerateSuggestions),
// so it needs a QueryClient in context.
function renderComposer(rt: SessionRuntime, mentionSource?: MentionSource[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><SessionComposer rt={rt} mentionSource={mentionSource} /></QueryClientProvider>,
  )
}

// Minimal runtime stub — only the fields SessionComposer reads. The point of this
// test is to lock the suggestions wiring so neither call site (mobile SessionView /
// desktop DesktopShell) can silently drop the prop again and break the ghost.
function fakeRuntime(over: Partial<SessionRuntime> = {}): SessionRuntime {
  return {
    sessionId: 's1',
    mode: 'default',
    onPrompt: vi.fn(),
    onInterrupt: vi.fn(),
    onSetMode: vi.fn(),
    onCancelQueued: vi.fn(),
    onCommand: vi.fn(),
    commands: [],
    chatArmed: false,
    suggestions: [],
    state: {
      status: { state: 'idle', count: 0 },
      queued: [],
      turns: [],
    },
    ...over,
  } as unknown as SessionRuntime
}

describe('SessionComposer (shared mobile/desktop wiring)', () => {
  it('forwards the runtime suggestions so the ghost renders', () => {
    // `rt.suggestions` is the runtime's ONE derivation (live event, else the record
    // cache) — the composer must read that, never `state.latestSuggestions`, or a
    // detached session's quick-reply menu shows a different set than its card.
    const rt = fakeRuntime({ suggestions: ['Run the tests'] })
    renderComposer(rt)
    expect(screen.getByTestId('ghost-suggestion')).toHaveTextContent('Run the tests')
  })

  it('renders no ghost when there are no suggestions', () => {
    renderComposer(fakeRuntime())
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })
})

describe('SessionComposer mentionSource wiring (spec v2 §10.2)', () => {
  it('forwards mentionSource so the assistant drawer can @ mention a session', async () => {
    const user = userEvent.setup()
    renderComposer(fakeRuntime(), [{ id: 's2', title: 'Fix the flaky test' }])
    await user.type(screen.getByLabelText('Message'), '@fi')
    expect(screen.getByRole('listbox', { name: /session mentions/i })).toHaveTextContent('Fix the flaky test')
  })

  it('a coding session (no mentionSource) never shows the mention menu', async () => {
    const user = userEvent.setup()
    renderComposer(fakeRuntime())
    await user.type(screen.getByLabelText('Message'), '@fi')
    expect(screen.queryByRole('listbox', { name: /session mentions/i })).not.toBeInTheDocument()
  })
})

describe('SessionComposer interrupt-and-send wiring', () => {
  it('routes Send now to the runtime onInterruptAndPrompt', () => {
    const onInterruptAndPrompt = vi.fn().mockReturnValue(true)
    const rt = fakeRuntime({
      onInterruptAndPrompt,
      state: { status: { state: 'in_progress', count: 0 }, queued: [], turns: [] },
    } as unknown as Partial<SessionRuntime>)
    renderComposer(rt)
    const textarea = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'now' } })
    fireEvent.click(screen.getByRole('button', { name: /send now/i }))
    expect(onInterruptAndPrompt).toHaveBeenCalledWith('now', [])
  })
})

it('forwards suggested provenance through the shared runtime adapter', () => {
  const rt = fakeRuntime({ suggestions: ['Keep going'] })
  renderComposer(rt)
  fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Keep going' }))
  expect(rt.onPrompt).toHaveBeenCalledWith('Keep going', [], undefined, 'suggested')
})
