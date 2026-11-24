/**
 * The Environment card's Remote Control row: on/off and whether it follows the default,
 * the switch's request and cache write, the "Open in Claude app" link, and a refusal
 * shown inline. Hidden by the card when the session does not report it available.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiRequestError } from '@/api/client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'
import { RemoteControlControls, type RemoteControlControlsProps } from './RemoteControlControls'
import { EnvironmentCard } from './EnvironmentCard'
import { REMOTE_CONTROL_ACCESS_NOTE, REMOTE_CONTROL_TOGGLE_FALLBACK } from '../remoteControlCopy'

const URL = 'https://claude.ai/code/session_abc'

let reply: unknown
let posts: { path: string; body: unknown }[]

beforeEach(() => {
  vi.restoreAllMocks()
  reply = { enabled: true, url: URL }
  posts = []
  vi.spyOn(api, 'get').mockResolvedValue([])
  vi.spyOn(api, 'post').mockImplementation(async (path: string, body?: unknown) => {
    posts.push({ path, body })
    if (reply instanceof Error) throw reply
    return reply
  })
})

function mount(props: Partial<RemoteControlControlsProps> = {}, qc = newClient()) {
  render(
    <QueryClientProvider client={qc}>
      <RemoteControlControls sessionId="s1" enabled={false} {...props} />
    </QueryClientProvider>,
  )
  return qc
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
}

const toggle = () => screen.getByRole('switch', { name: 'Remote Control' })

describe('RemoteControlControls', () => {
  it('says when it follows the Settings default', () => {
    mount({ enabled: true, override: null })
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('remote-control-state')).toHaveTextContent('On · Following the default')
  })

  it.each([
    [true, 'On · Set for this session'],
    [false, 'Off · Set for this session'],
  ])('says when the session overrides it (%s)', (value, label) => {
    mount({ enabled: value, override: value })
    expect(screen.getByTestId('remote-control-state')).toHaveTextContent(label)
  })

  it('links to the Claude app and notes who can drive it while on', () => {
    mount({ enabled: true, url: URL })
    const link = screen.getByRole('link', { name: /Open in Claude app/ })
    expect(link).toHaveAttribute('href', URL)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getByTestId('remote-control-note')).toHaveTextContent(REMOTE_CONTROL_ACCESS_NOTE)
  })

  it('offers no link while off, without a url, or for an unsafe url', () => {
    mount({ enabled: false, url: URL })
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.queryByTestId('remote-control-note')).toBeNull()
  })

  it.each([null, 'javascript:alert(1)'])('offers no link for url %s', (url) => {
    mount({ enabled: true, url })
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('turning it on posts the override and writes the answer into the cached session', async () => {
    const qc = newClient()
    qc.setQueryData<Partial<Session>[]>(QK.sessions, [{ id: 's1', remoteControl: false, remoteControlOverride: null }])
    mount({ enabled: false }, qc)
    await userEvent.click(toggle())
    expect(posts).toEqual([{ path: '/sessions/s1/remote-control', body: { enabled: true } }])
    await waitFor(() => {
      expect(qc.getQueryData<Session[]>(QK.sessions)?.[0]).toMatchObject({
        remoteControl: true, remoteControlOverride: true, remoteControlUrl: URL,
      })
    })
  })

  it('turning it off posts enabled: false', async () => {
    reply = { enabled: false, url: null }
    mount({ enabled: true, url: URL })
    await userEvent.click(toggle())
    expect(posts).toEqual([{ path: '/sessions/s1/remote-control', body: { enabled: false } }])
  })

  it('shows a refusal inline, in the server words or a plain line', async () => {
    reply = new ApiRequestError(409, { detail: { code: 'REMOTE_CONTROL_FAILED', message: 'The bridge did not answer' } })
    mount()
    await userEvent.click(toggle())
    expect(await screen.findByRole('alert')).toHaveTextContent('The bridge did not answer')
  })

  it('falls back to a plain line when the server says nothing', async () => {
    reply = new ApiRequestError(500, {})
    mount()
    await userEvent.click(toggle())
    expect(await screen.findByRole('alert')).toHaveTextContent(REMOTE_CONTROL_TOGGLE_FALLBACK)
  })
})

describe('EnvironmentCard — Remote Control row', () => {
  const base = { account: 'a', accounts: [{ name: 'a' }], accountPinned: false, onSelectAccount: () => {} }

  it('is absent unless passed (the session reports it unavailable)', () => {
    render(<QueryClientProvider client={newClient()}><EnvironmentCard {...base} /></QueryClientProvider>)
    expect(screen.queryByTestId('remote-control-controls')).toBeNull()
  })

  it('renders when passed', () => {
    render(
      <QueryClientProvider client={newClient()}>
        <EnvironmentCard {...base} remoteControl={{ sessionId: 's1', enabled: true, override: null, url: URL }} />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('remote-control-controls')).toBeInTheDocument()
  })
})
