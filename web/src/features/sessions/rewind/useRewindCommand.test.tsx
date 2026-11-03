/**
 * `/rewind` (D-9): the dock composer's onCommand opens a picker of the recent prompts,
 * newest first; picking one runs Rewind to here (dry run → confirm). Busy or with no
 * prompt it toasts and opens nothing; any other command, and a `rewind` that is not
 * ours, goes to the session.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, renderHook, act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { Command } from '@/session/events'
import type { Turn } from '@/session/reducer'
import type { SessionRuntime } from '@/session/useSessionRuntime'
import { useRewindCommand } from './useRewindCommand'

const REWIND: Command = { name: 'rewind', description: '', argumentHint: null, kind: 'client', source: 'app' }
const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const ask = (n: number): Turn => ({ id: uuid(n), role: 'user', blocks: [{ kind: 'text', id: `${n}:u`, text: `ask ${n}` }] })
const reply = (n: number): Turn => ({ id: `a${n}`, role: 'assistant', blocks: [{ kind: 'text', id: `${n}:a`, text: `answer ${n}` }] })

function rtWith(turns: Turn[], { commands = [REWIND], state = 'idle' }: { commands?: Command[]; state?: string } = {}) {
  const onCommand = vi.fn()
  const rt = { sessionId: 's1', commands, onCommand, state: { turns, status: { state, count: 0 } } } as unknown as SessionRuntime
  return { rt, onCommand }
}

const qc = () => new QueryClient({ defaultOptions: { mutations: { retry: false } } })
const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc()}>{children}</QueryClientProvider>

/** Renders the hook's UI next to a stand-in dock region, and hands back its onCommand. */
function mount(rt: SessionRuntime) {
  let run: SessionRuntime['onCommand'] = () => {}
  function Host() {
    const { onCommand, element } = useRewindCommand(rt)
    run = onCommand
    return <div data-region="app.dock">{element}</div>
  }
  render(<QueryClientProvider client={qc()}><Host /></QueryClientProvider>)
  return (name: string, args = '') => act(() => run(name, args))
}

describe('useRewindCommand', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

  it('opens a picker of the prompts, newest first and at most 20', async () => {
    const turns = Array.from({ length: 22 }, (_, i) => [ask(i), reply(i)]).flat()
    const { rt, onCommand } = rtWith(turns)
    const run = mount(rt)
    run('rewind')
    expect(await screen.findAllByRole('menuitem')).toHaveLength(20)
    const labels = screen.getAllByText(/^ask \d+$/).map((el) => el.textContent)
    expect(labels[0]).toBe('ask 21')
    expect(labels[19]).toBe('ask 2')
    expect(screen.getByRole('menu', { name: 'Rewind to a message' })).toBeTruthy()
    expect(onCommand).not.toHaveBeenCalled()
  })

  it('picking a prompt runs Rewind to here on it', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue(
      { files: ['a.ts'], deleted: [], insertions: 1, deletions: 0, checkpointAvailable: true })
    const { rt } = rtWith([ask(1), reply(1), ask(2), reply(2)])
    const run = mount(rt)
    run('rewind')
    await userEvent.click(await screen.findByRole('menuitem', { name: 'ask 1' }))
    expect(await screen.findByRole('dialog', { name: 'Rewind to this message?' })).toBeTruthy()
    expect(post).toHaveBeenCalledWith('/sessions/s1/rewind', expect.objectContaining({ toTurnId: uuid(1), dryRun: true }))
  })

  it('while busy, toasts why and opens nothing', async () => {
    const { rt } = rtWith([ask(1), reply(1)], { state: 'working' })
    const run = mount(rt)
    run('rewind')
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Claude is working or waiting on you — rewind once that's settled"))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it.each(['waiting_input', 'waiting_question'])('with a pending card (%s), toasts why and opens nothing', async (state) => {
    const { rt } = rtWith([ask(1), reply(1)], { state })
    const run = mount(rt)
    run('rewind')
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Claude is working or waiting on you — rewind once that's settled"))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('with no prompt to rewind to, toasts and opens nothing', async () => {
    const { rt } = rtWith([reply(1)])
    const run = mount(rt)
    run('rewind')
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Nothing to rewind to yet'))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('passes every other command, and a rewind the catalog did not make ours, to the session', () => {
    const { rt, onCommand } = rtWith([ask(1)], { commands: [{ ...REWIND, kind: 'passthrough', source: 'vendor' }] })
    const { result } = renderHook(() => useRewindCommand(rt), { wrapper })
    act(() => { result.current.onCommand('model', 'opus'); result.current.onCommand('rewind', '') })
    expect(onCommand).toHaveBeenNthCalledWith(1, 'model', 'opus')
    expect(onCommand).toHaveBeenNthCalledWith(2, 'rewind', '')
  })
})
