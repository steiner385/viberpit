/**
 * The Environment card's Sandbox row (D-23): the state, the toggle and why it is
 * disabled, the respawn warning in both directions, the full-auto note while on, and
 * every refusal shown inline.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiRequestError } from '@/api/client'
import type { SandboxFacts, SessionSandbox } from '@/api/types'
import { SandboxControls } from './SandboxControls'
import { EnvironmentCard } from './EnvironmentCard'
import {
  SANDBOX_BUSY, SANDBOX_CONFIRM, SANDBOX_DID_NOT_START, SANDBOX_ERROR_MESSAGES, SANDBOX_NOTE,
} from '../sandboxCopy'

const FACTS: SandboxFacts = {
  sessionId: 's1', host: 'dobby', capability: true, enabled: false, state: 'off', reason: null,
  held: false, busy: false, available: true, unavailableReason: null,
  preflight: { ok: true, reason: null, checks: [], cliVersion: '2.1.319' },
  canToggle: true, allowedDomains: ['github.com', '*.github.com', '*.githubusercontent.com'],
}
const OK = { sessionId: 's1', enabled: true, state: 'active', reason: null, held: false, respawned: true }

let facts: unknown
let reply: unknown
let posts: { path: string; body: unknown }[]

beforeEach(() => {
  vi.restoreAllMocks()
  facts = FACTS
  reply = OK
  posts = []
  vi.spyOn(api, 'get').mockImplementation(async () => {
    if (facts instanceof Error) throw facts
    return facts
  })
  vi.spyOn(api, 'post').mockImplementation(async (path: string, body?: unknown) => {
    posts.push({ path, body })
    if (reply instanceof Error) throw reply
    return reply
  })
})

function mount({ busy = false, state = null as SessionSandbox | null } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SandboxControls sessionId="s1" busy={busy} state={state} />
    </QueryClientProvider>,
  )
}

const toggle = () => screen.findByRole('switch', { name: 'Sandbox' })

describe('SandboxControls — state', () => {
  it.each([
    [{ enabled: false, state: 'off', reason: null }, 'Off'],
    [{ enabled: true, state: 'active', reason: null }, 'Active'],
    [{ enabled: true, state: 'pending', reason: null }, 'Starting…'],
    [{ enabled: true, state: 'failed', reason: 'socat is not installed' }, 'Failed: socat is not installed'],
  ] as const)('reads %o as "%s"', async (state, label) => {
    mount({ state: { ...state } })
    await toggle()
    expect(screen.getByTestId('sandbox-state')).toHaveTextContent(label)
  })

  it('falls back to the facts when the payload carries no sandbox', async () => {
    facts = { ...FACTS, enabled: true, state: 'active' }
    mount()
    expect(await toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('sandbox-state')).toHaveTextContent('Active')
  })

  it('shows the full-auto note only while on', async () => {
    const { unmount } = mount({ state: { enabled: true, state: 'active', reason: null } })
    await toggle()
    expect(screen.getByTestId('sandbox-note')).toHaveTextContent(SANDBOX_NOTE)
    expect(SANDBOX_NOTE).toBe('Network: GitHub only. Writes: this worktree only. In full-auto there are no prompts — the sandbox only contains commands.')
    unmount()
    mount({ state: { enabled: false, state: 'off', reason: null } })
    await toggle()
    expect(screen.queryByTestId('sandbox-note')).toBeNull()
  })

  it('renders nothing unless the facts confirm the capability (kill switch, Codex, a stale route)', async () => {
    for (const f of [{ ...FACTS, capability: false }, [], new ApiRequestError(404, { detail: 'session not found' })]) {
      facts = f
      const { container, unmount } = mount()
      await waitFor(() => expect(api.get).toHaveBeenCalled())
      expect(container).toBeEmptyDOMElement()
      unmount()
      vi.mocked(api.get).mockClear()
    }
  })
})

describe('SandboxControls — disabled reasons', () => {
  it('is disabled with the host preflight reason when the sandbox is unavailable', async () => {
    facts = { ...FACTS, available: false, canToggle: false, unavailableReason: 'socat is not installed' }
    mount()
    expect(await toggle()).toBeDisabled()
    expect(screen.getByTestId('sandbox-disabled-reason')).toHaveTextContent('socat is not installed')
    expect(await toggle()).toHaveAccessibleDescription(/socat is not installed/)
  })

  it('is disabled while the session is busy — live turn state or the facts', async () => {
    const { unmount } = mount({ busy: true })
    expect(await toggle()).toBeDisabled()
    expect(screen.getByTestId('sandbox-disabled-reason')).toHaveTextContent(SANDBOX_BUSY)
    unmount()
    facts = { ...FACTS, busy: true, canToggle: false }
    mount()
    expect(await toggle()).toBeDisabled()
    expect(screen.getByTestId('sandbox-disabled-reason')).toHaveTextContent('Busy — wait for the turn to finish')
  })

  it('re-reads the facts when a turn ends', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const ui = (busy: boolean) => (
      <QueryClientProvider client={qc}><SandboxControls sessionId="s1" busy={busy} /></QueryClientProvider>
    )
    const { rerender } = render(ui(true))
    await toggle()
    expect(api.get).toHaveBeenCalledTimes(1)
    rerender(ui(false))
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
  })
})

describe('SandboxControls — toggle', () => {
  it('turning on warns that it restarts the session, then posts enabled: true', async () => {
    mount()
    await userEvent.click(await toggle())
    const dialog = screen.getByRole('dialog', { name: SANDBOX_CONFIRM.on.title })
    expect(dialog).toHaveTextContent(/restarts the session/)
    expect(posts).toHaveLength(0)
    await userEvent.click(within(dialog).getByRole('button', { name: SANDBOX_CONFIRM.on.confirmLabel }))
    await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: true } }]))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('turning off says it restarts the session too', async () => {
    reply = { ...OK, enabled: false, state: 'off' }
    mount({ state: { enabled: true, state: 'active', reason: null } })
    await userEvent.click(await toggle())
    const dialog = screen.getByRole('dialog', { name: SANDBOX_CONFIRM.off.title })
    expect(dialog).toHaveTextContent(/restarts the session without the sandbox/)
    await userEvent.click(within(dialog).getByRole('button', { name: SANDBOX_CONFIRM.off.confirmLabel }))
    await waitFor(() => expect(posts).toEqual([{ path: '/sessions/s1/sandbox', body: { enabled: false } }]))
  })

  it('cancelling the warning posts nothing', async () => {
    mount()
    await userEvent.click(await toggle())
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(posts).toHaveLength(0)
  })

  it('a respawn whose sandbox failed says the session is held', async () => {
    reply = { ...OK, state: 'failed', reason: 'the sandbox did not start', held: true, respawned: false }
    mount()
    await userEvent.click(await toggle())
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: SANDBOX_CONFIRM.on.confirmLabel }))
    expect(await screen.findByTestId('sandbox-message')).toHaveTextContent(SANDBOX_DID_NOT_START)
  })

  it.each([
    [new ApiRequestError(409, { detail: { code: 'SANDBOX_UNAVAILABLE', message: 'm', reason: 'socat is not installed' } }),
      'The sandbox is unavailable on this host: socat is not installed'],
    [new ApiRequestError(409, { detail: { code: 'SANDBOX_SESSION_BUSY', message: 'm' } }), SANDBOX_ERROR_MESSAGES.SANDBOX_SESSION_BUSY],
    [new ApiRequestError(403, { detail: { code: 'SESSION_CONTROL_HUMAN_ONLY', message: 'm' } }), SANDBOX_ERROR_MESSAGES.SESSION_CONTROL_HUMAN_ONLY],
    [new ApiRequestError(409, { detail: { code: 'SESSION_MOVING', message: 'm', movingTo: 'rocky' } }), /rocky/],
    [new ApiRequestError(409, { detail: { code: 'SOMETHING_NEW', message: 'The server says no' } }), 'The server says no'],
  ])('shows a refusal inline (%#)', async (err, text) => {
    reply = err
    mount()
    await userEvent.click(await toggle())
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: SANDBOX_CONFIRM.on.confirmLabel }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(text)
  })
})

describe('EnvironmentCard — sandbox row', () => {
  const base = {
    account: 'work', accounts: [{ name: 'work' }], accountPinned: false, onSelectAccount: () => {},
  }

  it('is absent unless the card is handed the sandbox (capability off, or not Claude)', () => {
    render(<EnvironmentCard {...base} />)
    expect(screen.queryByTestId('sandbox-controls')).toBeNull()
    expect(api.get).not.toHaveBeenCalled()
  })

  it('shows the row when handed it', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <EnvironmentCard {...base} sandbox={{ sessionId: 's1', busy: false }} />
      </QueryClientProvider>,
    )
    expect(await toggle()).toBeEnabled()
  })
})
