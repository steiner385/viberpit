import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionRuntimeProvider, useSessionRuntimeContext } from '../SessionRuntimeProvider'

// Channels the provider asked `useSessionSocket` for, in mount order.
const channels: (string | undefined)[] = []

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: (channel?: string) => {
      channels.push(channel)
      return {
        attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
        sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
        setPermissionMode: vi.fn(), cancelQueued: vi.fn(), on: vi.fn(), off: vi.fn(),
        connectionState: 'connected' as const, socket: null,
      }
    },
  }
})

beforeEach(() => { channels.length = 0 })

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

  it('runs on the route channel by default', () => {
    render(
      <SessionRuntimeProvider sessionId="abcd1234efgh"><Probe /></SessionRuntimeProvider>,
      { wrapper },
    )
    expect(channels).toEqual(['route'])
  })

  it('forwards an explicit channel to the runtime (the AI drawer\'s second socket)', () => {
    render(
      <SessionRuntimeProvider sessionId="a1b2c3d4e5f6" channel="assistant"><Probe /></SessionRuntimeProvider>,
      { wrapper },
    )
    expect(channels).toEqual(['assistant'])
  })
})
