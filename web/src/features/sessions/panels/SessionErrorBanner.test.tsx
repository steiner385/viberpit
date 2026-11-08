/**
 * The session error banner, and the sandbox hold it carries (D-23): the reason, Retry,
 * and the confirmed "Turn off sandbox and continue".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiRequestError } from '@/api/client'
import { SessionErrorBanner, type SandboxHold } from './SessionErrorBanner'
import { sandboxFactsKey } from '@/api/sandboxHooks'
import { SANDBOX_CONFIRM, SANDBOX_ERROR_MESSAGES, SANDBOX_ESCAPE_COPY } from '../sandboxCopy'

type Err = { code: string; message: string } | null

let reply: unknown
let posts: { path: string; body: unknown }[]
let facts: unknown

beforeEach(() => {
  vi.restoreAllMocks()
  posts = []
  facts = { sessionId: 's1', capability: true, enabled: true, state: 'failed', reason: 'x', held: true, busy: false,
    available: true, unavailableReason: null, preflight: null, canToggle: true, allowedDomains: [] }
  vi.spyOn(api, 'get').mockImplementation(async () => {
    if (facts instanceof Error) throw facts
    return facts
  })
  reply = { sessionId: 's1', enabled: true, state: 'active', reason: null, held: false, respawned: true }
  vi.spyOn(api, 'post').mockImplementation(async (path: string, body?: unknown) => {
    posts.push({ path, body })
    if (reply instanceof Error) throw reply
    return reply
  })
})

function mount(error: Err, hold: SandboxHold | null = null, dismissError = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const rt = { state: { error } as never, dismissError }
  render(<QueryClientProvider client={qc}><SessionErrorBanner rt={rt} sandboxHold={hold} /></QueryClientProvider>)
  return { dismissError, qc }
}

const HOLD: SandboxHold = { sessionId: 's1', reason: 'socat is not installed' }

describe('SessionErrorBanner', () => {
  it('renders nothing with no error and no hold', () => {
    mount(null)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a dismissable error', async () => {
    const { dismissError } = mount({ code: 'x', message: 'Boom' })
    expect(screen.getByRole('alert')).toHaveTextContent('Error: Boom')
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(dismissError).toHaveBeenCalled()
  })
})

describe('SessionErrorBanner — sandbox hold', () => {
  it('states the reason with Retry and Turn off, and is not dismissable', async () => {
    mount(null, HOLD)
    const hold = await screen.findByTestId('sandbox-hold')
    expect(hold).toHaveTextContent('Sandbox failed — session held: socat is not installed')
    expect(await within(hold).findByRole('button', { name: 'Retry' })).toBeVisible()
    expect(within(hold).getByRole('button', { name: 'Turn off sandbox and continue' })).toBeVisible()
    expect(within(hold).queryByRole('button', { name: 'Dismiss' })).toBeNull()
  })

  it('stands in for the attach refusal that states the same fact', async () => {
    mount({ code: 'SESSION_SANDBOX_FAILED', message: 'The session is held…' }, HOLD)
    await screen.findByTestId('sandbox-hold')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('keeps an unrelated error beside the hold', async () => {
    mount({ code: 'other', message: 'Something else' }, HOLD)
    await screen.findByTestId('sandbox-hold')
    expect(screen.getAllByRole('alert')).toHaveLength(2)
  })

  it('Retry posts enabled: true at once', async () => {
    mount(null, HOLD)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: true } }]))
  })

  it('a Retry whose sandbox fails again says it is still held', async () => {
    reply = { sessionId: 's1', enabled: true, state: 'failed', reason: 'the sandbox did not start', held: true, respawned: false }
    mount(null, HOLD)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('sandbox-hold-error')).toHaveTextContent('Still held: the sandbox did not start')
  })

  it('Turn off asks first; cancelling posts nothing, confirming posts enabled: false', async () => {
    reply = { sessionId: 's1', enabled: false, state: 'off', reason: null, held: false, respawned: true }
    mount(null, HOLD)
    await userEvent.click(await screen.findByRole('button', { name: 'Turn off sandbox and continue' }))
    let dialog = screen.getByRole('dialog', { name: SANDBOX_CONFIRM.continueOff.title })
    expect(dialog).toHaveTextContent(/no longer contained/)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(posts).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Turn off sandbox and continue' }))
    dialog = screen.getByRole('dialog', { name: SANDBOX_CONFIRM.continueOff.title })
    await userEvent.click(within(dialog).getByRole('button', { name: SANDBOX_CONFIRM.continueOff.confirmLabel }))
    await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: false } }]))
  })

  it('kill switch off (capability false): no Retry, but Turn off and continue stays', async () => {
    facts = { ...(facts as object), capability: false, reason: 'the sandbox is turned off on this host' }
    reply = { sessionId: 's1', enabled: false, state: 'off', reason: null, held: false, respawned: true }
    const { qc } = mount(null, { sessionId: 's1', reason: 'the sandbox is turned off on this host' })
    const off = await screen.findByRole('button', { name: 'Turn off sandbox and continue' })
    // The facts have answered (so Retry's absence is the verdict, not a load).
    await waitFor(() => expect(qc.getQueryData(sandboxFactsKey('s1'))).toBeDefined())
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    await userEvent.click(off)
    await userEvent.click(within(screen.getByRole('dialog', { name: SANDBOX_CONFIRM.continueOff.title }))
      .getByRole('button', { name: SANDBOX_CONFIRM.continueOff.confirmLabel }))
    await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: false } }]))
  })

  it('a failed facts read still offers Retry (the server decides)', async () => {
    facts = new ApiRequestError(409, { detail: { code: 'daemon_outdated' } })
    mount(null, HOLD)
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeVisible()
  })

  describe('escape suspected (fix round 3)', () => {
    const ESCAPE: SandboxHold = { sessionId: 's1', reason: null, escaped: true }

    it('reads its own copy, with the same two actions', async () => {
      mount(null, ESCAPE)
      const hold = await screen.findByTestId('sandbox-hold')
      expect(hold).toHaveTextContent(SANDBOX_ESCAPE_COPY)
      expect(SANDBOX_ESCAPE_COPY).toBe(
        "A file write went outside this session's worktree, so the session was stopped. Check the worktree before continuing.")
      expect(hold).not.toHaveTextContent('Sandbox failed')
      expect(await within(hold).findByRole('button', { name: 'Retry' })).toBeVisible()
      expect(within(hold).getByRole('button', { name: 'Turn off sandbox and continue' })).toBeVisible()
    })

    it('Retry releases it with enabled: true', async () => {
      mount(null, ESCAPE)
      await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
      await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: true } }]))
    })

    it('Turn off goes through the shared confirm, then enabled: false', async () => {
      reply = { sessionId: 's1', enabled: false, state: 'off', reason: null, held: false, respawned: true }
      mount(null, ESCAPE)
      await userEvent.click(await screen.findByRole('button', { name: 'Turn off sandbox and continue' }))
      const dialog = screen.getByRole('dialog', { name: SANDBOX_CONFIRM.continueOff.title })
      await userEvent.click(within(dialog).getByRole('button', { name: SANDBOX_CONFIRM.continueOff.confirmLabel }))
      await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: false } }]))
    })
  })

  it('shows a refusal inline', async () => {
    reply = new ApiRequestError(409, { detail: { code: 'SANDBOX_SESSION_BUSY', message: 'm' } })
    mount(null, HOLD)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('sandbox-hold-error')).toHaveTextContent(SANDBOX_ERROR_MESSAGES.SANDBOX_SESSION_BUSY)
  })
})
