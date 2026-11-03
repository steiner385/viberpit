/**
 * The ⌘K palette through the REAL shell (D-10): the chord, the lazy mount, the
 * keyboard, and that every entry lands where the shell's own path goes. Only
 * the api client and the session socket are faked — the process boundary —
 * the same harness as `shell/useShortcuts.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
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
  // A quiet maintenance session: it lives in the Background scope, so the
  // Active list's Project filter never offers its repo.
  session({
    id: 'bg1', name: 'Dependency sweep', repo: 'acme/bots', branch: 'chore/deps', status: 'in_progress',
    workClass: 'maintenance', updatedAt: '2026-10-20T10:00:00Z',
  }),
]

const ARCHIVED: ArchivedSession[] = [{
  id: 'old1', name: 'Ancient voyage', account: 'alice', cwd: '/x', repo: null, engine: 'claude_code',
  engineSessionId: null, permissionMode: 'default', createdAt: '2026-10-01T10:00:00Z', archivedAt: '2026-10-02T10:00:00Z',
}, {
  id: 'old2', name: 'Lonely relic', account: 'alice', cwd: '/x', repo: 'acme/lonely', engine: 'claude_code',
  engineSessionId: null, permissionMode: 'default', createdAt: '2026-09-01T10:00:00Z', archivedAt: '2026-09-02T10:00:00Z',
}]

const REPOS = ['acme/rocket', 'acme/lonely', 'acme/bots'].map((nameWithOwner) => ({
  nameWithOwner, name: nameWithOwner.split('/')[1], owner: 'acme', isPrivate: false, url: '',
  defaultBranch: 'main', pushedAt: null, cloned: true,
}))

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

// Counts how often the palette re-prepares its entries — the cost an
// unmemoized host would pay on every shell render while the palette is open.
const prepareCalls = vi.hoisted(() => ({ n: 0 }))
vi.mock('./paletteSearch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./paletteSearch')>()
  return {
    ...actual,
    prepareSearch: (...args: Parameters<typeof actual.prepareSearch>) => {
      prepareCalls.n += 1
      return actual.prepareSearch(...args)
    },
  }
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
const listSearch = () => screen.getByRole('searchbox', { name: 'Search sessions' })
/** The labels of the Sessions section's options (first line of each row). */
const sessionOptions = () => {
  const group = within(palette()!).queryAllByRole('group').find((g) => g.getAttribute('aria-labelledby')
    && document.getElementById(g.getAttribute('aria-labelledby')!)?.textContent === 'Sessions')
  return group ? within(group).getAllByRole('option').map((o) => o.firstElementChild?.firstElementChild?.textContent) : []
}
/** Select an option with the keyboard: walk to it, then Enter. */
async function chooseOption(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
  const target = within(palette()!).getByRole('option', { name })
  for (let i = 0; i < 50 && target.getAttribute('aria-selected') !== 'true'; i++) await user.keyboard('{ArrowDown}')
  await user.keyboard('{Enter}')
}
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
    // `ui/Dialog` refuses modals in kiosk, so "no dialog" alone would pass with
    // the row's `interactive` guard deleted. The dispatcher `preventDefault`s
    // every row it fires, synchronously — so an UNprevented keydown is the
    // proof that no row (the palette's included) claimed the chord.
    renderApp({ path: '/pipeline', mode: 'kiosk' })
    const notPrevented = fireEvent.keyDown(document.body, { key: 'k', code: 'KeyK', ctrlKey: true })
    expect(notPrevented).toBe(true)
    expect(palette()).not.toBeInTheDocument()
  })
})

describe('cost while open', () => {
  it('a shell re-render neither re-prepares the entries nor re-scrolls the selection', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const user = userEvent.setup()
    const app = await renderList()
    await openFromRail(user)
    await within(palette()!).findByRole('option', { name: /Ancient voyage/ })
    const prepared = prepareCalls.n
    const scrolled = scroll.mock.calls.length
    // The shell re-renders on every route change; the palette's inputs did not change.
    await act(async () => { await app.router.navigate('/sessions?probe=1') })
    await act(async () => { await app.router.navigate('/sessions?probe=2') })
    expect(palette()).toBeInTheDocument()
    expect(prepareCalls.n).toBe(prepared)
    expect(scroll.mock.calls.length).toBe(scrolled)
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
    expect(options().slice(0, 4).map((o) => o.textContent)).toEqual([
      expect.stringContaining('One'), expect.stringContaining('Two'),
      expect.stringContaining('Dependency sweep'), expect.stringContaining('Ancient voyage'),
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

  it('a repo the Project filter offers applies it, clearing the list search', async () => {
    const user = userEvent.setup()
    await renderList()
    await user.type(listSearch(), 'stale')
    await openFromRail(user)
    await user.keyboard('acme/rocket')
    await user.click(await within(palette()!).findByRole('option', { name: /^acme\/rocket$/ }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('acme/rocket'))
    expect(listSearch()).toHaveValue('')
    // The search's debounce has to settle before the rows come back.
    expect(await screen.findByLabelText('Open session One')).toBeInTheDocument()
    expect(screen.queryByLabelText('Open session Two')).not.toBeInTheDocument()
  })

  it('a repo with no live session drills down inside the palette — the list is untouched', async () => {
    const user = userEvent.setup()
    const app = await renderList()
    await openFromRail(user)
    await user.keyboard('acme/lonely')
    await within(palette()!).findByRole('option', { name: /^acme\/lonely$/ })
    await chooseOption(user, /^acme\/lonely$/)
    expect(palette()).toBeInTheDocument()
    expect(paletteInput()).toHaveValue('acme/lonely')
    expect(paletteInput()).toHaveFocus()
    expect(sessionOptions()).toEqual(['Lonely relic'])
    expect(app.path()).toBe('/sessions')
    expect(listSearch()).toHaveValue('')
    expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('')
  })

  it('a background-only repo drills down to its background session', async () => {
    const user = userEvent.setup()
    await renderList()
    await openFromRail(user)
    await user.keyboard('acme/bots')
    await within(palette()!).findByRole('option', { name: /^acme\/bots$/ })
    await chooseOption(user, /^acme\/bots$/)
    expect(paletteInput()).toHaveValue('acme/bots')
    expect(sessionOptions()).toEqual(['Dependency sweep'])
  })

  it('a branch drills down, from any section, without touching the list search', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/pipeline', mode: 'desktop' })
    await openFromRail(user)
    await user.keyboard('feat/thrust')
    await within(palette()!).findByRole('option', { name: /^feat\/thrust$/ })
    await chooseOption(user, /^feat\/thrust$/)
    expect(paletteInput()).toHaveValue('feat/thrust')
    expect(sessionOptions()).toEqual(['One'])
    expect(app.path()).toBe('/pipeline')
  })

  it('with a Project filter for repo A, a branch of repo B never leaves the list filtered to A', async () => {
    const user = userEvent.setup()
    const app = await renderList()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Project' }), 'acme/rocket')
    expect(screen.queryByLabelText('Open session Two')).not.toBeInTheDocument()
    await openFromRail(user)
    await user.keyboard('main')
    await within(palette()!).findByRole('option', { name: /^main$/ })
    await chooseOption(user, /^main$/)
    expect(sessionOptions()).toEqual(['Two'])
    await user.keyboard('{Enter}')
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
    // The session it opened is in the list it sits beside, not filtered out of it.
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue(''))
    expect(screen.getByLabelText('Open session Two')).toBeInTheDocument()
  })
})
