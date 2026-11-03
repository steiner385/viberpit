/**
 * `/fork` (D-8): the dock composer's onCommand forks at the latest settled reply,
 * toasts and sends nothing with no reply to fork at, and leaves every other
 * command — and a vendor `fork` that is not ours — to the session.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
const navigate = vi.fn()
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { Command } from '@/session/events'
import type { Turn } from '@/session/reducer'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { useForkCommand } from './useForkFrom'

const FORK: Command = { name: 'fork', description: '', argumentHint: null, kind: 'client', source: 'app' }

function rtWith(turns: Turn[], commands: Command[] = [FORK]) {
  const onCommand = vi.fn()
  const rt = { sessionId: 's1', commands, onCommand, state: { turns } } as unknown as SessionRuntime
  return { rt, onCommand }
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return <QueryClientProvider client={qc}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
}

const anchored = (id: string): Turn => ({ id, role: 'assistant', anchorUuid: `a-${id}`, blocks: [] })

describe('useForkCommand', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

  it('forks at the latest anchored reply and opens the child', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ sessionId: 'child', prefill: null })
    const { rt, onCommand } = rtWith([anchored('t1'), { id: 'u2', role: 'user', blocks: [] }, anchored('t2'),
      { id: 't3', role: 'assistant', blocks: [] }])
    const { result } = renderHook(() => useForkCommand(rt), { wrapper })
    act(() => result.current('fork', ''))
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/sessions/child'))
    expect(post).toHaveBeenCalledWith('/sessions/s1/fork', expect.objectContaining({ atTurnId: 't2', mode: 'reply' }))
    expect(onCommand).not.toHaveBeenCalled()
  })

  it('with no settled reply, toasts and makes no request', async () => {
    const post = vi.spyOn(api, 'post')
    const { rt, onCommand } = rtWith([{ id: 't1', role: 'assistant', blocks: [] }])
    const { result } = renderHook(() => useForkCommand(rt), { wrapper })
    act(() => result.current('fork', ''))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Nothing to fork yet — /fork needs a finished reply'))
    expect(post).not.toHaveBeenCalled()
    expect(onCommand).not.toHaveBeenCalled()
  })

  it('passes every other command, and a fork the catalog did not make ours, to the session', () => {
    const post = vi.spyOn(api, 'post')
    const { rt, onCommand } = rtWith([anchored('t1')], [{ ...FORK, kind: 'passthrough', source: 'vendor' }])
    const { result } = renderHook(() => useForkCommand(rt), { wrapper })
    act(() => { result.current('model', 'opus'); result.current('fork', '') })
    expect(onCommand).toHaveBeenNthCalledWith(1, 'model', 'opus')
    expect(onCommand).toHaveBeenNthCalledWith(2, 'fork', '')
    expect(post).not.toHaveBeenCalled()
  })
})
