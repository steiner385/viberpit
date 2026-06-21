import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionRuntimeProvider, useSessionRuntimeContext } from '../SessionRuntimeProvider'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

function Probe() {
  const rt = useSessionRuntimeContext()
  return <div>title:{rt.title}</div>
}
function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('SessionRuntimeProvider', () => {
  it('provides runtime to descendants (title defaults to sessionId prefix)', () => {
    render(
      <SessionRuntimeProvider sessionId="abcd1234efgh"><Probe /></SessionRuntimeProvider>,
      { wrapper },
    )
    expect(screen.getByText('title:abcd1234')).toBeInTheDocument()
  })
})
