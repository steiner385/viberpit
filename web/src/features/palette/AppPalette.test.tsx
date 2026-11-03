/**
 * The ⌘K palette through the REAL shell (D-10): the chord, the lazy mount, the
 * keyboard, and that every entry lands where the shell's own path goes. Only
 * the api client and the session socket are faked — the process boundary —
 * the same harness as `shell/useShortcuts.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ArchivedSession, Session } from '@/api/types'

function session(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'One', accountName: 'alice', engine: 'claude_code', engineSessionId: null,
    status: 'idle', createdAt: '2026-06-19T10:00:00Z', prs: [], ...over,
  }
}

const SESSIONS: Session[] = [
  session({ id: 's1', name: 'One', repo: 'acme/rocket', branch: 'feat/thrust', updatedAt: '2026-11-02T10:00:00Z' }),
  session({ id: 's2', name: 'Two', repo: 'acme/other', branch: 'main', updatedAt: '2026-11-01T10:00:00Z' }),
]

const ARCHIVED: ArchivedSession[] = [{
  id: 'old1', name: 'Ancient voyage', account: 'alice', cwd: '/x', repo: null, engine: 'claude_code',
  engineSessionId: null, permissionMode: 'default', createdAt: '2026-10-01T10:00:00Z', archivedAt: '2026-10-02T10:00:00Z',
}]

const REPOS = [{
  nameWithOwner: 'acme/rocket', name: 'rocket', owner: 'acme', isPrivate: false, url: '',
  defaultBranch: 'main', pushedAt: null, cloned: true,
}]

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/sessions' || path === '/sessions?viewing=1') return SESSIONS
        if (path === '/sessions/archived') return ARCHIVED
        if (path === '/github/repos') return REPOS
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/settings') return { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }
        if (path === '/daemon/health') return { available: true }
        return []
      }),
      post: vi.fn(async () => ({})),
      put: vi.fn(async () => ({})),
      patch: vi.fn(async () => ({})),
      del: vi.fn(async () => undefined),
    },
  }
})

vi.stubGlobal('EventSource', class {
  onopen: (() => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close() {}
  addEventListener() {}
  removeEventListener() {}
})

const socket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(() => true), cancelQueued: vi.fn(), command: vi.fn(),
  activity: vi.fn(), on: vi.fn(), off: vi.fn(),
  connectionState: 'connected' as const, socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return { ...actual, useSessionSocket: () => socket }
})

import { renderApp } from '@/test/renderApp'
import { resetOverlayState } from '@/shell/useOverlayState'

const palette = () => screen.queryByRole('dialog', { name: 'Command palette' })
const findPalette = () => screen.findByRole('dialog', { name: 'Command palette' })
const paletteInput = () => screen.getByRole('combobox', { name: /search sessions, actions and settings/i })
const railLink = () => screen.getByRole('link', { name: /^Sessions/ })
const composer = () => screen.getByRole('textbox', { name: /^message$/i }) as HTMLTextAreaElement
const helpDrawer = () => screen.queryByRole('dialog', { name: /help/i })
const options = () => within(palette()!).queryAllByRole('option')
const groupNames = () => within(palette()!).getAllByRole('group').map((g) => g.textContent?.split(/(?=[A-Z][a-z])/)[0])

async function renderList() {
  const app = renderApp({ path: '/sessions', mode: 'desktop' })
  await screen.findByLabelText('Open session One')
  return app
}

async function openFromRail(user: ReturnType<typeof userEvent.setup>) {
  railLink().focus()
  await user.keyboard('{Control>}k{/Control}')
  await findPalette()
  await waitFor(() => expect(paletteInput()).toHaveFocus())
}

beforeEach(() => {
  resetOverlayState()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('⌘K / Ctrl+K', () => {
  it('Ctrl+K opens the palette with the search focused; Ctrl+K again closes it and focus returns', async () => {
    const user = userEvent.setup()
    await renderList()
    await openFromRail(user)
    await user.keyboard('{Control>}k{/Control}')
    await waitFor(() => expect(palette()).not.toBeInTheDocument())
    expect(railLink()).toHaveFocus()
  })

  it('⌘K opens it from the session composer mid-draft; Esc closes it and the draft and focus come back', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions/s1', mode: 'desktop' })
    await screen.findByRole('button', { name: /back to session list/i })
    await user.click(composer())
    await user.type(composer(), 'wip')
    await user.keyboard('{Meta>}k{/Meta}')
    await findPalette()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(palette()).not.toBeInTheDocument())
    expect(composer()).toHaveValue('wip')
    expect(composer()).toHaveFocus()
    // Esc closed the palette only: the session is still open.
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
  })

  it('traps focus: Tab and Shift+Tab stay inside the palette', async () => {
    const user = userEvent.setup()
    await renderList()
    await openFromRail(user)
    await user.tab()
    expect(palette()!.contains(document.activeElement)).toBe(true)
    await user.tab({ shift: true })
    expect(palette()!.contains(document.activeElement)).toBe(true)
  })

  it('does nothing in kiosk — the row never fires, not merely a refused dialog', async () => {
    // `ui/Dialog` refuses modals in kiosk (and warns), so "no dialog" alone
    // would pass with the row's `interactive` guard deleted. The warning — the
    // lazy palette mounting at all — is the guard's observable.
    // The chunk is warmed first so a palette that DID mount would do so well
    // inside the wait below.
    await import('@/features/palette')
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const user = userEvent.setup()
    renderApp({ path: '/pipeline', mode: 'kiosk' })
    await user.keyboard('{Control>}k{/Control}')
    // Past React's 300 ms Suspense reveal throttle.
    await new Promise((r) => setTimeout(r, 800))
    expect(palette()).not.toBeInTheDocument()
    expect(warn.mock.calls.flat().join(' ')).not.toMatch(/Command palette/)
    warn.mockRestore()
  })
})

describe('the list', () => {
  it('an empty query shows recent sessions, then Actions', async () => {
    const user = userEvent.setup()
    await renderList()
    await openFromRail(user)
    await waitFor(() => expect(within(palette()!).getByRole('option', { name: /Ancient voyage/ })).toBeInTheDocument())
    expect(groupNames()).toEqual(['Sessions', 'Actions'])
    // Most recent first.
    expect(options().slice(0, 3).map((o) => o.textContent)).toEqual([
      expect.stringContaining('One'), expect.stringContaining('Two'), expect.stringContaining('Ancient voyage'),
    ])
  })

  it('↑/↓ move the selection (wrapping) and Enter opens the selected session', async () => {
    const user = userEvent.setup()
    const app = await renderList()
    await openFromRail(user)
    await waitFor(() => expect(options()[0]).toHaveAttribute('aria-selected', 'true'))
    await user.keyboard('{ArrowDown}')
    expect(options()[1]).toHaveAttribute('aria-selected', 'true')
    expect(paletteInput()).toHaveAttribute('aria-activedescendant', options()[1].id)
    await user.keyboard('{ArrowUp}{ArrowUp}')
    expect(options()[options().length - 1]).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
    expect(palette()).not.toBeInTheDocument()
  })

  it('type, Enter: lands on the session', async () => {
    const user = userEvent.setup()
    const app = await renderList()
    await openFromRail(user)
    await user.keyboard('two')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
  })

  it('archived sessions are searchable and open in the archived reader', async () => {
    const user = userEvent.setup()
    const app = await renderList()
    await openFromRail(user)
    await user.keyboard('ancient')
    await waitFor(() => expect(options()[0]).toHaveTextContent('Ancient voyage'))
    await user.keyboard('{Enter}')
    await waitFor(() => expect(app.path()).toBe('/sessions/archived/old1'))
  })

  it('an action runs the shortcut’s own handler: “Open this help panel” opens help', async () => {
    const user = userEvent.setup()
    await renderList()
    await openFromRail(user)
    await user.keyboard('help panel')
    const opt = await within(palette()!).findByRole('option', { name: /open this help panel/i })
    expect(opt).toHaveTextContent(/Ctrl\+\/|⌘\//)
    await user.keyboard('{Enter}')
    await waitFor(() => expect(helpDrawer()).toBeInTheDocument())
  })

  it('a settings section navigates to it', async () => {
    const user = userEvent.setup()
    const app = await renderList()
    await openFromRail(user)
    await user.keyboard('mcp servers{Enter}')
    await waitFor(() => expect(app.path()).toBe('/settings/mcp'))
  })

  it('a repo applies the list’s Project filter', async () => {
    const user = userEvent.setup()
    await renderList()
    await openFromRail(user)
    await user.keyboard('acme/rocket')
    const opt = await within(palette()!).findByRole('option', { name: /^acme\/rocket$/ })
    await user.click(opt)
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('acme/rocket'))
    expect(screen.queryByLabelText('Open session Two')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Open session One')).toBeInTheDocument()
  })

  it('a branch sets the list search, from another section', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/pipeline', mode: 'desktop' })
    await openFromRail(user)
    await user.keyboard('feat/thrust')
    const opt = await within(palette()!).findByRole('option', { name: /^feat\/thrust$/ })
    await user.click(opt)
    await waitFor(() => expect(app.path()).toBe('/sessions'))
    await waitFor(() => expect(screen.getByRole('searchbox', { name: 'Search sessions' })).toHaveValue('feat/thrust'))
  })
})
