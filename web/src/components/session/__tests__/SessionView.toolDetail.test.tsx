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

describe('SessionView tool-detail control removed from the top bar', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders no tool-detail toggle button in the session top bar', () => {
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    expect(screen.queryByRole('button', { name: /tool calls/i })).not.toBeInTheDocument()
  })

  it('pressing "c" does not produce a tool-detail toggle button', async () => {
    const user = userEvent.setup()
    render(<SessionView sessionId="s1" onBack={vi.fn()} />, { wrapper })
    await user.keyboard('c')
    expect(screen.queryByRole('button', { name: /tool calls/i })).not.toBeInTheDocument()
  })
})
