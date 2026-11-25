/**
 * The Sessions list's Remote group: shown only when the feature is on and a remote
 * session exists, server order kept, rows link to the remote page, "Open in Claude
 * app" only for a safe http(s) url.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import type { RemoteSession, RemoteSessionsList } from '@/api/remoteSessionsHooks'
import { RemoteSessionsSection } from './RemoteSessionsSection'

const h = vi.hoisted(() => ({ data: undefined as RemoteSessionsList | undefined }))
vi.mock('@/api/remoteSessionsHooks', async (orig) => ({
  ...(await orig<typeof import('@/api/remoteSessionsHooks')>()),
  useRemoteSessions: () => ({ data: h.data }),
}))

function session(over: Partial<RemoteSession> = {}): RemoteSession {
  return {
    id: 'session_a', account: 'alice', title: 'Fix the flaky test', status: 'idle',
    connection: 'connected', machineId: 'abcdef1234567890', origin: 'repl', cwd: '/w/app',
    model: 'claude-opus', url: 'https://claude.ai/code/session_a', updatedAt: '2026-11-25T10:00:00Z',
    needsYou: false, unread: false, ...over,
  }
}

function renderAt(path = '/sessions') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <RemoteSessionsSection />
    </MemoryRouter>,
  )
}

beforeEach(() => { h.data = undefined })

describe('RemoteSessionsSection', () => {
  it('renders nothing before the list answers', () => {
    const { container } = renderAt()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the feature is switched off', () => {
    h.data = { enabled: false, sessions: [], accounts: [] }
    const { container } = renderAt()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when there are no remote sessions', () => {
    h.data = { enabled: true, sessions: [], accounts: [{ account: 'alice', status: 'ok' }] }
    const { container } = renderAt()
    expect(container).toBeEmptyDOMElement()
  })

  it('lists the sessions in server order with origin, machine and status', () => {
    h.data = {
      enabled: true, accounts: [],
      sessions: [
        session({ id: 'session_b', title: 'Needs approval', status: 'requires_action', needsYou: true, origin: 'sdk' }),
        session({ id: 'session_a', title: 'Working away', status: 'running', origin: 'repl' }),
        session({ id: 'session_c', title: 'Gone quiet', connection: 'disconnected', origin: null, machineId: null }),
      ],
    }
    renderAt()
    expect(screen.getByRole('button', { name: /remote \(3\)/i })).toHaveAttribute('aria-expanded', 'true')
    // The header calls out how many need the user.
    expect(screen.getByText('1 need you')).toBeInTheDocument()
    const rows = screen.getAllByTestId('remote-session-row')
    expect(rows.map((r) => within(r).getAllByRole('link')[0].textContent)).toEqual([
      expect.stringContaining('Needs approval'),
      expect.stringContaining('Working away'),
      expect.stringContaining('Gone quiet'),
    ])
    expect(within(rows[0]).getByText('SDK · machine abcdef12')).toBeInTheDocument()
    expect(within(rows[0]).getByText('Needs you')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Claude Code CLI · machine abcdef12')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Working')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Connected')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Disconnected', { selector: '[class*="rounded-pill"]' })).toBeInTheDocument()
  })

  it('links each row to its remote page and marks the open one', () => {
    h.data = { enabled: true, accounts: [], sessions: [session(), session({ id: 'session_z', title: 'Other' })] }
    renderAt('/sessions/remote/session_z')
    const open = screen.getByRole('link', { name: /^other/i })
    expect(open).toHaveAttribute('href', '/sessions/remote/session_z')
    expect(open).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: /^fix the flaky test/i })).not.toHaveAttribute('aria-current')
  })

  it('offers "Open in Claude app" only for a safe http(s) url', () => {
    h.data = {
      enabled: true, accounts: [],
      sessions: [
        session({ id: 'session_ok', title: 'Safe' }),
        session({ id: 'session_js', title: 'Hostile', url: 'javascript:alert(1)' }),
        session({ id: 'session_none', title: 'No link', url: null }),
      ],
    }
    renderAt()
    const appLink = screen.getByRole('link', { name: 'Open Safe in Claude app' })
    expect(appLink).toHaveAttribute('href', 'https://claude.ai/code/session_a')
    expect(appLink).toHaveAttribute('target', '_blank')
    expect(appLink).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.queryByRole('link', { name: /open hostile in claude app/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /open no link in claude app/i })).toBeNull()
    expect(document.querySelector('a[href^="javascript"]')).toBeNull()
  })

  it('collapses and expands the group', async () => {
    const user = userEvent.setup()
    h.data = { enabled: true, accounts: [], sessions: [session()] }
    renderAt()
    const toggle = screen.getByRole('button', { name: /remote \(1\)/i })
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('remote-session-row')).toBeNull()
    await user.click(toggle)
    expect(screen.getAllByTestId('remote-session-row')).toHaveLength(1)
  })
})
