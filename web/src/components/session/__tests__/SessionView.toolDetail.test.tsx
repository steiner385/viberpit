import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionView } from '../SessionView'

const stableMockSocket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
  on: vi.fn(), off: vi.fn(), connectionState: 'connected' as const, socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return { ...actual, useSessionSocket: () => stableMockSocket }
})

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('SessionView tool-detail toggle', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders a collapse-tool-calls button that flips its label when clicked', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    const btn = screen.getByRole('button', { name: /collapse tool calls/i })
    await user.click(btn)
    expect(screen.getByRole('button', { name: /expand tool calls/i })).toBeInTheDocument()
  })

  it('pressing "c" toggles the tool-detail button label', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    expect(screen.getByRole('button', { name: /collapse tool calls/i })).toBeInTheDocument()
    // The composer textarea is auto-focused on entering a session. The global
    // "c" shortcut is intentionally suppressed while a text input is focused
    // (SessionView's `typing` guard), so blur the composer to exercise it.
    ;(document.activeElement as HTMLElement | null)?.blur()
    await user.keyboard('c')
    expect(screen.getByRole('button', { name: /expand tool calls/i })).toBeInTheDocument()
  })

  it('cycles through the full three-way toggle: collapse → expand → auto-fold → collapse', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    // Initial state: auto → button says "Collapse tool calls"
    expect(screen.getByRole('button', { name: /collapse tool calls/i })).toBeInTheDocument()
    // First click: auto → all-closed → button says "Expand tool calls"
    await user.click(screen.getByRole('button', { name: /collapse tool calls/i }))
    expect(screen.getByRole('button', { name: /expand tool calls/i })).toBeInTheDocument()
    // Second click: all-closed → all-open → button says "Auto-fold tool calls"
    await user.click(screen.getByRole('button', { name: /expand tool calls/i }))
    expect(screen.getByRole('button', { name: /auto-fold tool calls/i })).toBeInTheDocument()
    // Third click: all-open → auto → button says "Collapse tool calls" again
    await user.click(screen.getByRole('button', { name: /auto-fold tool calls/i }))
    expect(screen.getByRole('button', { name: /collapse tool calls/i })).toBeInTheDocument()
  })
})
