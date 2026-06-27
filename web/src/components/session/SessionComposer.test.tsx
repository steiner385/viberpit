import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionComposer } from './SessionComposer'
import type { SessionRuntime } from '@/session/useSessionRuntime'

// SessionComposer now owns the on-demand suggestion mutation (useGenerateSuggestions),
// so it needs a QueryClient in context.
function renderComposer(rt: SessionRuntime) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><SessionComposer rt={rt} /></QueryClientProvider>,
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
    state: {
      status: { state: 'idle', count: 0 },
      queued: [],
      turns: [],
      latestSuggestions: [],
    },
    ...over,
  } as unknown as SessionRuntime
}

describe('SessionComposer (shared mobile/desktop wiring)', () => {
  it('forwards latestSuggestions so the ghost renders', () => {
    const rt = fakeRuntime({
      state: { status: { state: 'idle', count: 0 }, queued: [], turns: [], latestSuggestions: ['Run the tests'] },
    } as unknown as Partial<SessionRuntime>)
    renderComposer(rt)
    expect(screen.getByTestId('ghost-suggestion')).toHaveTextContent('Run the tests')
  })

  it('renders no ghost when there are no suggestions', () => {
    renderComposer(fakeRuntime())
    expect(screen.queryByTestId('ghost-suggestion')).toBeNull()
  })
})
