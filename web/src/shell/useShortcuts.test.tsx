/**
 * The app's global keyboard gestures, asserted as BEHAVIOUR through the real
 * shell.
 *
 * ## Why this file was written BEFORE the code it guards
 *
 * PR 4 replaces five window/document `keydown` listeners with one dispatcher in
 * `shell/useShortcuts.ts`. A keyboard listener that silently stops firing looks
 * exactly like one that was never installed, so every case below was written and
 * RUN against the pre-cutover app (`useGlobalHelpKey`, `useGlobalListNav`,
 * `useInSessionKeys`, and the two overflow-menu document listeners) and then run
 * again, unchanged, against the single dispatcher. Nothing here names a hook, a
 * listener or a registry row: each case presses a key and asserts what the user
 * sees — the drawer, the highlight, the URL, the menu — so it holds across the
 * swap rather than describing whichever implementation is current.
 *
 * The one case that did NOT pass before the cutover is marked in place, with
 * what it did instead.
 *
 * The real shell, the real route table, the real pages: only the daemon (the api
 * client) and the session websocket are faked, because they are the process
 * boundary. The list, the composer, the drawer host and the nav rail are all the
 * shipping components.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Session } from '@/api/types'

// ── the fake daemon (same shape as AppShell.test.tsx: the CLIENT, not the hooks) ──

function session(over: Partial<Session> = {}): Session {
  return {
    id: 's1',
    name: 'One',
    accountName: 'alice',
    engine: 'claude_code',
    engineSessionId: null,
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    prs: [],
    ...over,
  }
}

const SESSIONS: Session[] = [session({ id: 's1', name: 'One' }), session({ id: 's2', name: 'Two' })]

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/sessions' || path === '/sessions?viewing=1') return SESSIONS
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        if (path === '/settings') {
          return { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }
        }
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

// jsdom has no `EventSource`, and the shell's badge/pipeline stream opens one on
// mount. A no-op class (never a cast — R10) is enough: nothing here asserts on
// the stream.
vi.stubGlobal('EventSource', class {
  onopen: (() => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close() {}
  addEventListener() {}
  removeEventListener() {}
})

// ── the fake session socket ──────────────────────────────────────────────────
//
// `on` is captured so a test can deliver a permission request and make
// `hasOpenAction` true — the state the Esc/← guard and the Ctrl+Z escape hatch
// are entirely about.

let handlers: Record<string, ((...args: unknown[]) => void)[]> = {}

const socket = {
  attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
  sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
  setPermissionMode: vi.fn(() => true), cancelQueued: vi.fn(), command: vi.fn(),
  activity: vi.fn(),
  on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
    ;(handlers[event] ??= []).push(cb)
  }),
  off: vi.fn(),
  connectionState: 'connected' as const,
  socket: null,
}

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return { ...actual, useSessionSocket: () => socket }
})

// sonner's `toast` is a spy over the real one: the shell's own Toaster is mounted
// by `main.tsx`, outside `renderApp`, so the call is what is observable here.
const toastSpy = vi.hoisted(() => vi.fn())
vi.mock('sonner', async (importOriginal) => {
  const actual = await importOriginal<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(toastSpy, actual.toast) }
})

import { renderApp } from '@/test/renderApp'
import { MEDIA } from '@/test/matchMedia'
import { resetOverlayState } from './useOverlayState'

// ── helpers ──────────────────────────────────────────────────────────────────

const helpDrawer = () => screen.queryByRole('dialog', { name: /help/i })
const railLink = () => screen.getByRole('link', { name: /^Sessions/ })
const composer = () => screen.getByRole('textbox', { name: /^message$/i }) as HTMLTextAreaElement
const row = (name: string) => screen.getByLabelText(`Open session ${name}`)

/** Deliver an unanswered permission card into the open session. */
async function openPermissionCard(): Promise<void> {
  await act(async () => {
    const emit = (frame: Record<string, unknown>) =>
      handlers['sessionEvent']?.forEach((cb) => cb(frame))
    emit({ type: 'replayComplete' })
    emit({ type: 'turnStart', turnId: 't1' })
    emit({
      type: 'permissionRequest', permId: 'perm-1', turnId: 't1',
      tool: 'bash', toolKind: 'bash', input: { command: 'ls' }, plan: false,
    })
  })
}

/** The shell on the open session `s1`, desktop (list beside the session). */
async function renderSession() {
  const view = renderApp({ path: '/sessions/s1', mode: 'desktop' })
  await waitFor(() => expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument())
  return view
}

beforeEach(() => {
  handlers = {}
  resetOverlayState()
  window.localStorage.clear()
  // `renderApp({ mode: 'kiosk' })` puts `?kiosk=1` on the real `window.location`
  // (a wall display is configured by the URL it was launched with, so
  // `readKioskConfig` reads that and not the router's). It outlives the test, so
  // clear it here or every case after the kiosk one silently renders a kiosk.
  window.history.replaceState(null, '', '/')
})

// ── `?` — the help drawer ────────────────────────────────────────────────────

describe('? opens the help drawer', () => {
  it('from anywhere in the chrome', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    await user.keyboard('?')
    expect(helpDrawer()).toBeInTheDocument()
  })

  it('on Shift+/ too — the key a browser may report instead of "?"', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    await user.keyboard('{Shift>}/{/Shift}')
    expect(helpDrawer()).toBeInTheDocument()
  })

  it('NOT while typing in a text box — "?" is a character there', async () => {
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    await user.keyboard('?')
    expect(helpDrawer()).not.toBeInTheDocument()
    expect(composer()).toHaveValue('?')
  })

  // The fixture used to be the New Session dialog, which was retired with
  // `?dialog=new`. An OVERLAY-mode `Sheet` is the better stand-in anyway: it is
  // the same modal focus trap as far as `overlayIsOpen()` is concerned, and it
  // is the exact surface the pushed-drawer case below contrasts against, so the
  // two tests now differ only in the media query — which is the real rule.
  it('NOT while a modal overlay is open', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    await waitFor(() => expect(screen.getByRole('dialog', { name: /assistant/i })).toHaveAttribute('aria-modal', 'true'))
    await user.keyboard('?')
    expect(helpDrawer()).not.toBeInTheDocument()
  })

  // R5: a PUSHED drawer is a region, not an overlay. It carries `role="dialog"`
  // but no `aria-modal`, and it sits beside the content rather than over it, so
  // having the assistant docked open on a wide screen must not make `?` dead for
  // the whole app.
  it('YES while a docked (push-mode) drawer is open — it is a region, not an overlay', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop', extraQueries: [MEDIA.drawerPush] })
    await waitFor(() => expect(screen.getByRole('dialog', { name: /assistant/i })).toBeInTheDocument())
    railLink().focus()
    await user.keyboard('?')
    expect(helpDrawer()).toBeInTheDocument()
  })

  it('NOT in kiosk — a wall display has no drawer to open into', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'kiosk' })
    await user.keyboard('?')
    expect(helpDrawer()).not.toBeInTheDocument()
    // Added in Task 5, as a sibling of the same defect found in the new ⌘/ case:
    // kiosk hides `app.drawer` outright, so "no dialog in the document" is true
    // whether or not the row's `interactive` guard did anything. `?drawer=` is
    // the guard's only observable, so it is the one asserted.
    expect(app.search()).not.toMatch(/drawer=/)
  })
})

// ── the app-global chords: ⌘/ Ctrl+/ and ⌘; Ctrl+; (Task 5) ─────────────────
//
// The two Desktop-parity bindings that survive contact with a browser. The
// other four (⌘N, ⌘W, Ctrl+Tab, Ctrl+Shift+Tab) are reserved by the browser's
// own UI and are asserted to bind NOTHING, at the bottom of this file.

const assistantDrawer = () => screen.queryByRole('dialog', { name: /assistant/i })

describe('⌘/ · Ctrl+/ opens the help drawer', () => {
  it('on Ctrl+/ — the non-Apple binding', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    await user.keyboard('{Control>}/{/Control}')
    expect(helpDrawer()).toBeInTheDocument()
  })

  it('on ⌘/ — the Apple binding, accepted on every platform', async () => {
    // The matcher takes either modifier rather than sniffing the platform: a
    // Mac on an external PC keyboard and a Linux box with ⌘ mapped to Super
    // both report a platform that disagrees with the keys in front of the user.
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    await user.keyboard('{Meta>}/{/Meta}')
    expect(helpDrawer()).toBeInTheDocument()
  })

  it('YES mid-draft in the composer — a chord is not a character there', async () => {
    // The contrast that justifies a second row rather than a widened
    // `compose.help`: three cases up, `?` typed into this same box produced the
    // character and no drawer.
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    await user.type(composer(), 'wip')
    await user.keyboard('{Meta>}/{/Meta}')
    expect(helpDrawer()).toBeInTheDocument()
    expect(composer()).toHaveValue('wip')
  })

  it('NOT in kiosk — a wall display has no drawer region to open into', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'kiosk' })
    await user.keyboard('{Meta>}/{/Meta}')
    await user.keyboard('{Control>}/{/Control}')
    expect(helpDrawer()).not.toBeInTheDocument()
    // The URL, not just the absence of a node. `REGION_VISIBILITY['app.drawer']`
    // is false in kiosk, so the drawer would be missing from the document even
    // if the row HAD fired and written `?drawer=help` — the assertion above
    // passes for a reason that has nothing to do with the guard under test.
    // (Verified: deleting `c.interactive` from the row's `when` left it green.)
    // `?drawer=` is the row's only observable effect, so that is what is asserted.
    expect(app.search()).not.toMatch(/drawer=/)
  })
})

describe('⌘; · Ctrl+; opens the assistant drawer', () => {
  it('on Ctrl+;', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    await user.keyboard('{Control>};{/Control}')
    await waitFor(() => expect(assistantDrawer()).toBeInTheDocument())
  })

  it('on ⌘;', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    await user.keyboard('{Meta>};{/Meta}')
    await waitFor(() => expect(assistantDrawer()).toBeInTheDocument())
  })

  it('NOT in kiosk', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'kiosk' })
    await user.keyboard('{Meta>};{/Meta}')
    await user.keyboard('{Control>};{/Control}')
    expect(assistantDrawer()).not.toBeInTheDocument()
    // See the help row's kiosk case: the missing node proves nothing here, the
    // un-written `?drawer=` param does.
    expect(app.search()).not.toMatch(/drawer=/)
  })
})

// ── the Desktop chords a browser reserves ───────────────────────────────────
//
// jsdom reserves nothing: it delivers ⌘W and Ctrl+Tab to the page exactly as it
// delivers any other key. A real Chrome or Safari does NOT — it never dispatches
// the keydown for a shortcut its own UI owns, so `preventDefault()` is not just
// ignored, it is never reached. These cases therefore cannot prove the browser
// withholds the key. They prove OUR half: nothing in the app acts on them, so
// the help sheet is not printing a promise the browser eats.

describe('the browser-reserved Desktop chords do nothing here', () => {
  it('⌘W / Ctrl+W does not detach — the browser closes the tab instead', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.keyboard('{Meta>}w{/Meta}')
    expect(app.path()).toBe('/sessions/s1')
    await user.keyboard('{Control>}w{/Control}')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('Ctrl+Tab / Ctrl+Shift+Tab does not cycle sessions — the browser cycles tabs', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.keyboard('{Control>}{Tab}{/Control}')
    expect(app.path()).toBe('/sessions/s1')
    await user.keyboard('{Control>}{Shift>}{Tab}{/Shift}{/Control}')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('⌘N / Ctrl+N does not open the New Session dialog — the browser opens a window', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'desktop' })
    await waitFor(() => expect(row('One')).toBeInTheDocument())
    await user.keyboard('{Meta>}n{/Meta}')
    await user.keyboard('{Control>}n{/Control}')
    expect(screen.queryByRole('dialog', { name: /new session/i })).not.toBeInTheDocument()
    expect(app.path()).toBe('/sessions')
    expect(app.search()).toBe('')
  })
})

// ── ↑/↓ — the session-list highlight ─────────────────────────────────────────

describe('↑/↓ move the list highlight', () => {
  it('with focus parked in the chrome rather than the prompt', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    await waitFor(() => expect(row('One')).toBeInTheDocument())
    railLink().focus()
    await user.keyboard('{ArrowDown}')
    expect(row('One')).toHaveAttribute('data-selected', 'true')
    await user.keyboard('{ArrowDown}')
    expect(row('Two')).toHaveAttribute('data-selected', 'true')
    await user.keyboard('{ArrowUp}')
    expect(row('One')).toHaveAttribute('data-selected', 'true')
  })

  it('NOT while a modal overlay is open', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions?drawer=assistant', mode: 'desktop' })
    await waitFor(() => expect(screen.getByRole('dialog', { name: /assistant/i })).toHaveAttribute('aria-modal', 'true'))
    await user.keyboard('{ArrowDown}')
    expect(row('One')).not.toHaveAttribute('data-selected', 'true')
    expect(row('Two')).not.toHaveAttribute('data-selected', 'true')
  })

  it('NOT while focus is in a form control — the control owns its arrows', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })
    await waitFor(() => expect(row('One')).toBeInTheDocument())
    screen.getByLabelText('Repository').focus()
    await user.keyboard('{ArrowDown}')
    expect(row('One')).not.toHaveAttribute('data-selected', 'true')
    expect(row('Two')).not.toHaveAttribute('data-selected', 'true')
  })

  it('NOT when something has already handled the key', async () => {
    renderApp({ path: '/sessions', mode: 'desktop' })
    await waitFor(() => expect(row('One')).toBeInTheDocument())
    railLink().focus()
    const e = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true })
    e.preventDefault()
    act(() => { document.activeElement?.dispatchEvent(e) })
    expect(row('One')).not.toHaveAttribute('data-selected', 'true')
  })
})

// ── the in-session detach gestures ───────────────────────────────────────────

describe('Esc / ← / Ctrl+Z detach from an open session', () => {
  it('Esc detaches back to the list', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    screen.getByRole('button', { name: /back to session list/i }).focus()
    await user.keyboard('{Escape}')
    expect(app.path()).toBe('/sessions')
  })

  it('Esc does NOT detach while a permission card is unanswered — answer it first', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await openPermissionCard()
    await user.keyboard('{Escape}')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('Esc does NOT detach while the composer holds a draft', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(composer())
    await user.type(composer(), 'wip')
    await user.keyboard('{Escape}')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('Ctrl+Z force-detaches EVEN with a permission card open — the escape hatch', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await openPermissionCard()
    await user.keyboard('{Control>}z{/Control}')
    expect(app.path()).toBe('/sessions')
  })

  it('← detaches from the composer’s left edge', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(composer())
    await user.keyboard('{ArrowLeft}')
    expect(app.path()).toBe('/sessions')
  })

  it('← does NOT detach from mid-prompt — it moves the caret', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(composer())
    await user.type(composer(), 'wip')
    await user.keyboard('{ArrowLeft}')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('← detaches from the left edge even mid-draft (the composer keeps its draft)', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    const box = composer()
    await user.click(box)
    await user.type(box, 'wip')
    box.setSelectionRange(0, 0)
    await user.keyboard('{ArrowLeft}')
    expect(app.path()).toBe('/sessions')
  })
})

// ── Esc stops a running turn (Desktop parity, sub-spec D §3) ─────────────────

/** Put the open session mid-turn: the runtime status the composer's Stop button reads. */
async function startTurn(): Promise<void> {
  await act(async () => {
    const emit = (frame: Record<string, unknown>) =>
      handlers['sessionEvent']?.forEach((cb) => cb(frame))
    emit({ type: 'replayComplete' })
    emit({ type: 'status', state: 'in_progress', count: 1 })
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop current turn' })).toBeInTheDocument())
}

describe('Esc stops a running turn; ← always detaches', () => {
  beforeEach(() => {
    socket.sendInterrupt.mockClear()
    toastSpy.mockClear()
  })

  it('busy: Esc interrupts the turn, says so, and stays in the session', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    screen.getByRole('button', { name: /back to session list/i }).focus()
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).toHaveBeenCalledWith('s1')
    expect(toastSpy).toHaveBeenCalledWith('Stopped · Esc')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('idle: Esc detaches, exactly as before', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    screen.getByRole('button', { name: /back to session list/i }).focus()
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).not.toHaveBeenCalled()
    expect(app.path()).toBe('/sessions')
  })

  it('busy: ← still detaches', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    screen.getByRole('button', { name: /back to session list/i }).focus()
    await user.keyboard('{ArrowLeft}')
    expect(socket.sendInterrupt).not.toHaveBeenCalled()
    expect(app.path()).toBe('/sessions')
  })

  it('busy + slash palette open: Esc closes the palette, nothing is interrupted', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    await act(async () => {
      handlers['commands']?.forEach((cb) => cb({
        sessionId: 's1',
        commands: [{ name: 'goal', description: 'Set the goal', argumentHint: null, kind: 'passthrough', source: 'vendor' }],
      }))
    })
    await user.click(composer())
    await user.type(composer(), '/go')
    expect(screen.getByRole('listbox', { name: /slash commands/i })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('listbox', { name: /slash commands/i })).not.toBeInTheDocument()
    expect(socket.sendInterrupt).not.toHaveBeenCalled()
    expect(app.path()).toBe('/sessions/s1')
  })

  it('busy + an unanswered permission card: Esc neither interrupts nor detaches', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    await openPermissionCard()
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).not.toHaveBeenCalled()
    expect(app.path()).toBe('/sessions/s1')
  })

  it('busy + the list search focused with text: Esc is the field\'s — no interrupt, no detach', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    const search = screen.getByRole('searchbox', { name: /search sessions/i })
    await user.click(search)
    await user.type(search, 'abc')
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).not.toHaveBeenCalled()
    expect(app.path()).toBe('/sessions/s1')
  })

  it('busy + the EMPTY list search focused: Esc still is not a stop — no interrupt, no detach', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    const search = screen.getByRole('searchbox', { name: /search sessions/i })
    await user.click(search)
    expect(search).toHaveValue('')
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).not.toHaveBeenCalled()
    expect(app.path()).toBe('/sessions/s1')
  })

  it('busy + the EMPTY composer focused: Esc interrupts', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    await user.click(composer())
    expect(composer().value).toBe('')
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).toHaveBeenCalledWith('s1')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('busy + focus on the page body: Esc interrupts', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).toHaveBeenCalledWith('s1')
    expect(app.path()).toBe('/sessions/s1')
  })

  it('busy + a composer draft: Esc interrupts regardless, and the draft is kept', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await startTurn()
    await user.click(composer())
    await user.type(composer(), 'wip')
    await user.keyboard('{Escape}')
    expect(socket.sendInterrupt).toHaveBeenCalledWith('s1')
    expect(composer().value).toBe('wip')
    expect(app.path()).toBe('/sessions/s1')
  })
})

// ── Alt+Shift+M / I / E open the prompt row's menus ──────────────────────────

describe('Alt+Shift+M / I / E open the prompt row menus', () => {
  const chord = (code: string, key: string) =>
    ({ key, code, altKey: true, shiftKey: true, bubbles: true, cancelable: true })

  it('Alt+Shift+M opens the mode menu while the composer is focused and typing', async () => {
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    await user.keyboard('hi')
    fireEvent.keyDown(composer(), chord('KeyM', 'M'))
    const menu = await screen.findByRole('menu')
    expect(within(menu).getByText('Ask')).toBeInTheDocument()
    expect(composer().value).toBe('hi')
  })

  it('Alt+Shift+I opens the model menu', async () => {
    await renderSession()
    fireEvent.keyDown(composer(), chord('KeyI', 'I'))
    const menu = await screen.findByRole('menu')
    expect(within(menu).queryByText('Ask')).not.toBeInTheDocument()
  })

  it('Alt+Shift+E does nothing on a Claude session (no effort picker)', async () => {
    await renderSession()
    fireEvent.keyDown(composer(), chord('KeyE', 'E'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('does nothing without a session open', async () => {
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    fireEvent.keyDown(railLink(), chord('KeyM', 'M'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})

// ── ⌘. / Ctrl+. — deny the oldest pending permission (D-2) ──────────────────

/** Deliver permission cards into the open session, oldest first. */
async function deliverCards(cards: { permId: string; plan: boolean }[]): Promise<void> {
  await act(async () => {
    const emit = (frame: Record<string, unknown>) =>
      handlers['sessionEvent']?.forEach((cb) => cb(frame))
    emit({ type: 'replayComplete' })
    emit({ type: 'turnStart', turnId: 't1' })
    for (const c of cards) {
      emit({
        type: 'permissionRequest', permId: c.permId, turnId: 't1',
        tool: 'bash', toolKind: 'bash', input: { command: `ls ${c.permId}` }, plan: c.plan,
      })
    }
  })
}

describe('⌘. / Ctrl+. denies the oldest pending permission', () => {
  beforeEach(() => { socket.permissionResponse.mockClear() })

  it('denies only the OLDEST of two cards — even with the composer focused', async () => {
    const user = userEvent.setup()
    await renderSession()
    await deliverCards([{ permId: 'perm-1', plan: false }, { permId: 'perm-2', plan: false }])
    await user.click(composer())
    await user.type(composer(), 'wip')
    await user.keyboard('{Control>}.{/Control}')
    expect(socket.permissionResponse).toHaveBeenCalledTimes(1)
    expect(socket.permissionResponse).toHaveBeenCalledWith('s1', 'perm-1', 'deny')
    expect(composer()).toHaveValue('wip')
  })

  it('on ⌘. too', async () => {
    const user = userEvent.setup()
    await renderSession()
    await deliverCards([{ permId: 'perm-1', plan: false }])
    await user.keyboard('{Meta>}.{/Meta}')
    expect(socket.permissionResponse).toHaveBeenCalledWith('s1', 'perm-1', 'deny')
  })

  it('does nothing — and claims nothing — with no card pending', async () => {
    await renderSession()
    composer().focus()
    const notPrevented = fireEvent.keyDown(composer(), { key: '.', ctrlKey: true })
    expect(notPrevented).toBe(true)
    expect(socket.permissionResponse).not.toHaveBeenCalled()
  })

  it('skips a plan card — a plan step is not a permission to deny', async () => {
    await renderSession()
    await deliverCards([{ permId: 'plan-1', plan: true }])
    composer().focus()
    const notPrevented = fireEvent.keyDown(composer(), { key: '.', ctrlKey: true })
    expect(notPrevented).toBe(true)
    expect(socket.permissionResponse).not.toHaveBeenCalled()
  })

  it('denies the permission card behind an older plan card, never the plan', async () => {
    const user = userEvent.setup()
    await renderSession()
    await deliverCards([{ permId: 'plan-1', plan: true }, { permId: 'perm-2', plan: false }])
    await user.click(composer())
    await user.keyboard('{Control>}.{/Control}')
    expect(socket.permissionResponse).toHaveBeenCalledTimes(1)
    expect(socket.permissionResponse).toHaveBeenCalledWith('s1', 'perm-2', 'deny')
  })
})

// ── Ctrl/⌘+O cycles the transcript view mode ─────────────────────────────────

describe('Ctrl/⌘+O cycles the transcript view mode', () => {
  const viewTrigger = () => screen.getByRole('button', { name: /^View:/ })

  it('cycles Normal → Thinking → Verbose → Normal on the open session', async () => {
    const user = userEvent.setup()
    await renderSession()
    expect(viewTrigger()).toHaveAccessibleName('View: Normal')
    railLink().focus()
    await user.keyboard('{Control>}o{/Control}')
    expect(viewTrigger()).toHaveAccessibleName('View: Thinking')
    await user.keyboard('{Meta>}o{/Meta}')
    expect(viewTrigger()).toHaveAccessibleName('View: Verbose')
    await user.keyboard('{Control>}o{/Control}')
    expect(viewTrigger()).toHaveAccessibleName('View: Normal')
  })

  it('still cycles while typing in the session composer, and keeps the draft', async () => {
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    await user.type(composer(), 'wip')
    const ev = new KeyboardEvent('keydown', { key: 'o', ctrlKey: true, bubbles: true, cancelable: true })
    act(() => { composer().dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(true)
    expect(viewTrigger()).toHaveAccessibleName('View: Thinking')
    expect(composer().value).toBe('wip')
  })

  it('does nothing with the assistant drawer open over the session', async () => {
    renderApp({ path: '/sessions/s1?drawer=assistant', mode: 'desktop' })
    await waitFor(() => expect(screen.getByRole('dialog', { name: /assistant/i })).toHaveAttribute('aria-modal', 'true'))
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'o', ctrlKey: true, bubbles: true, cancelable: true })
    expect(screen.getByRole('button', { name: /^View:/, hidden: true })).toHaveAccessibleName('View: Normal')
  })

  it('does nothing from another text entry (the list search); the session composer still cycles', async () => {
    const user = userEvent.setup()
    await renderSession()
    const search = screen.getByRole('searchbox', { name: /search sessions/i })
    await user.click(search)
    await user.type(search, 'abc')
    const fromSearch = new KeyboardEvent('keydown', { key: 'o', ctrlKey: true, bubbles: true, cancelable: true })
    act(() => { search.dispatchEvent(fromSearch) })
    expect(fromSearch.defaultPrevented).toBe(false)
    expect(viewTrigger()).toHaveAccessibleName('View: Normal')
    await user.click(composer())
    await user.keyboard('{Control>}o{/Control}')
    expect(viewTrigger()).toHaveAccessibleName('View: Thinking')
  })

  it('does nothing without a session open', async () => {
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    const ev = new KeyboardEvent('keydown', { key: 'o', ctrlKey: true, bubbles: true, cancelable: true })
    act(() => { railLink().dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(false)
  })
})

// ── an open menu owns Escape ─────────────────────────────────────────────────

describe('Escape closes an open menu instead of detaching', () => {
  it('a session row’s ⋯ menu', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await waitFor(() => expect(row('Two')).toBeInTheDocument())
    const cards = screen.getAllByRole('button', { name: /more actions/i })
    await user.click(cards[0])
    expect(screen.getAllByRole('menu').length).toBeGreaterThan(0)
    await user.keyboard('{Escape}')
    expect(screen.queryAllByRole('menu')).toHaveLength(0)
    expect(app.path()).toBe('/sessions/s1')
  })

  /**
   * The two cases either side of this one open the menu with `userEvent.click`,
   * which ALWAYS focuses its target — so they cannot fail on the defect below.
   *
   * A real browser delivers keydown to `document.activeElement`, and on macOS
   * Safari and Firefox clicking a `<button>` does not move focus to it. The menu's
   * Escape lives on a container `onKeyDown`, which only sees keys whose target is
   * inside that container, so unless the component focuses something inside itself
   * on open, Escape is delivered to `<body>` and nothing happens — while the open
   * menu's overlay registration keeps suppressing every global shortcut. The
   * keyboard is silently inert until the user clicks elsewhere.
   *
   * `fireEvent.click` reproduces that: it dispatches the click without focusing.
   * The second Escape is the assertion that matters most — it proves the app's
   * whole keyboard came back, not just that this one menu closed.
   */
  it('a ⋯ menu opened by a click that did NOT focus — Escape still closes it, and the keyboard is live again', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await waitFor(() => expect(row('Two')).toBeInTheDocument())
    const trigger = screen.getAllByRole('button', { name: /more actions/i })[0]

    fireEvent.click(trigger)
    expect(screen.getAllByRole('menu').length).toBeGreaterThan(0)

    // userEvent aims at document.activeElement — wherever the component left it.
    await user.keyboard('{Escape}')
    expect(screen.queryAllByRole('menu')).toHaveLength(0)
    expect(app.path()).toBe('/sessions/s1')

    // The overlay is released, so the next Escape reaches the shell and detaches.
    await user.keyboard('{Escape}')
    expect(app.path()).toBe('/sessions')
  })

  it('the session header’s ⋯ menu opened without focus — same, for the other menu', async () => {
    const user = userEvent.setup()
    const app = await renderSession()

    fireEvent.click(screen.getByRole('button', { name: /more session actions/i }))
    expect(screen.getByRole('menu', { name: /session actions/i })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: /session actions/i })).not.toBeInTheDocument()
    expect(app.path()).toBe('/sessions/s1')

    await user.keyboard('{Escape}')
    expect(app.path()).toBe('/sessions')
  })

  it('the session header’s ⋯ menu', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(screen.getByRole('button', { name: /more session actions/i }))
    expect(screen.getByRole('menu', { name: /session actions/i })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: /session actions/i })).not.toBeInTheDocument()
    expect(app.path()).toBe('/sessions/s1')
  })

  /**
   * PRE-CUTOVER FAILURE, recorded rather than adjusted.
   *
   * Against the five listeners this case FAILED: `ui/Menu` closes on Escape and
   * calls `preventDefault()`, but `useInSessionKeys`' window listener never
   * looked at `defaultPrevented`, so the same Escape that dismissed the theme
   * menu also walked out of the session. The two overflow menus above only
   * escaped that fate because their `document` listeners called
   * `stopPropagation()`, which the `ui/Menu` primitive does not.
   *
   * The cutover fixes it twice over: the dispatcher bails on a
   * `defaultPrevented` event, and an open `Menu` counts as an overlay
   * (`useOverlayState`), which the detach rows refuse to fire under.
   */
  it('the theme menu — closing it does not also leave the session', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(screen.getByRole('button', { name: /^theme$/i }))
    expect(screen.getByRole('menu', { name: /theme/i })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: /theme/i })).not.toBeInTheDocument()
    expect(app.path()).toBe('/sessions/s1')
  })
})

// ── D-10: session switching and prompt jumps ─────────────────────────────────
//
// Elsewhere: Alt+[ ] / Alt+Shift+1–9 / Alt+↑↓. On Apple: ⌘⌥[ ] / ⌘⌥1–9 / ⌘⌥↑↓
// — a bare ⌥ chord there is typing (German ⌥5 is "[") or caret movement (⌥↑).

/** Dispatch a keydown on `target` and return it, to read `defaultPrevented`. */
function press(target: Element, init: KeyboardEventInit): KeyboardEvent {
  const ev = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })
  act(() => { target.dispatchEvent(ev) })
  return ev
}

const PLATFORM = window.navigator.platform
function setPlatform(value: string): void {
  Object.defineProperty(window.navigator, 'platform', { value, configurable: true })
}

describe('session switching — Linux / Windows chords', () => {
  beforeEach(() => setPlatform('Linux x86_64'))
  afterEach(() => setPlatform(PLATFORM))
  const alt = (target: Element, key: string, code: string) => press(target, { key, code, altKey: true })

  it('Alt+] / Alt+[ step from the open session, wrapping at both ends', async () => {
    const app = await renderSession()
    alt(railLink(), ']', 'BracketRight')
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
    alt(railLink(), ']', 'BracketRight')
    await waitFor(() => expect(app.path()).toBe('/sessions/s1'))
    alt(railLink(), '[', 'BracketLeft')
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
  })

  it('the header chevrons go exactly where Alt+[ / Alt+] go', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(screen.getByRole('button', { name: 'Next session' }))
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
    const byChevron = app.path()
    await act(async () => { await app.router.navigate('/sessions/s1') })
    alt(railLink(), ']', 'BracketRight')
    await waitFor(() => expect(app.path()).toBe(byChevron))
    await act(async () => { await app.router.navigate('/sessions/s1') })
    await user.click(screen.getByRole('button', { name: 'Previous session' }))
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
  })

  it('Alt+Shift+2 opens the second session from inside the session composer', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(composer())
    const ev = press(composer(), { key: '@', code: 'Digit2', altKey: true, shiftKey: true })
    expect(ev.defaultPrevented).toBe(true)
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
  })

  it('bare Alt+2 is left to the browser (Linux tab select)', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(composer())
    const ev = press(composer(), { key: '2', code: 'Digit2', altKey: true })
    expect(ev.defaultPrevented).toBe(false)
    expect(app.path()).toBe('/sessions/s1')
  })

  it('Alt+] does nothing from another text entry (the list search)', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    const search = screen.getByRole('searchbox', { name: /search sessions/i })
    await user.click(search)
    const ev = alt(search, ']', 'BracketRight')
    expect(ev.defaultPrevented).toBe(false)
    expect(app.path()).toBe('/sessions/s1')
  })

  it('Alt+↑ / Alt+↓ are claimed with a session open — from the composer too', async () => {
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    for (const key of ['ArrowUp', 'ArrowDown']) {
      expect(press(composer(), { key, code: key, altKey: true }).defaultPrevented, `Alt+${key}`).toBe(true)
    }
  })

  it('Alt+↑ does nothing without a session open', () => {
    renderApp({ path: '/sessions', mode: 'desktop' })
    railLink().focus()
    expect(press(railLink(), { key: 'ArrowUp', code: 'ArrowUp', altKey: true }).defaultPrevented).toBe(false)
  })
})

describe('session switching — Apple chords (⌘⌥)', () => {
  beforeEach(() => setPlatform('MacIntel'))
  afterEach(() => setPlatform(PLATFORM))
  const cmdOpt = (target: Element, key: string, code: string) =>
    press(target, { key, code, metaKey: true, altKey: true })

  it('⌘⌥] / ⌘⌥[ step the list; ⌘⌥2 opens the second session from the composer', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    cmdOpt(railLink(), '‘', 'BracketRight')
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
    cmdOpt(railLink(), '“', 'BracketLeft')
    await waitFor(() => expect(app.path()).toBe('/sessions/s1'))
    await user.click(composer())
    expect(cmdOpt(composer(), '™', 'Digit2').defaultPrevented).toBe(true)
    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
  })

  it('⌘⌥↑ / ⌘⌥↓ are claimed from the composer', async () => {
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    for (const key of ['ArrowUp', 'ArrowDown']) {
      expect(cmdOpt(composer(), key, key).defaultPrevented, `⌘⌥${key}`).toBe(true)
    }
  })

  it('a bare ⌥5 typing "[" (German layout) in the composer is neither matched nor prevented', async () => {
    const user = userEvent.setup()
    const app = await renderSession()
    await user.click(composer())
    const ev = press(composer(), { key: '[', code: 'Digit5', altKey: true })
    expect(ev.defaultPrevented).toBe(false)
    expect(app.path()).toBe('/sessions/s1')
  })

  it('a bare ⌥↑ in the composer is left to the caret', async () => {
    const user = userEvent.setup()
    await renderSession()
    await user.click(composer())
    expect(press(composer(), { key: 'ArrowUp', code: 'ArrowUp', altKey: true }).defaultPrevented).toBe(false)
    expect(press(railLink(), { key: ']', code: 'BracketRight', altKey: true }).defaultPrevented).toBe(false)
  })
})

// ── D-11: ⌘F / Ctrl+F searches the open transcript ───────────────────────────
//
// Only from the transcript, the session composer, or nowhere (`document.body`):
// anywhere else the browser's own find must still work, so the key is neither
// matched nor prevented.

describe('⌘F / Ctrl+F opens the transcript search', () => {
  const searchBar = () => screen.queryByRole('searchbox', { name: 'Search this transcript' })
  const find = (target: Element, mod: 'ctrlKey' | 'metaKey' = 'ctrlKey') =>
    press(target, { key: 'f', code: 'KeyF', [mod]: true })

  /** The open session with a transcript on screen (the empty placeholder is not one). */
  async function renderTranscript(path = '/sessions/s1') {
    const view = renderApp({ path, mode: 'desktop' })
    await waitFor(() => expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument())
    await act(async () => {
      const emit = (frame: Record<string, unknown>) => handlers['sessionEvent']?.forEach((cb) => cb(frame))
      emit({ type: 'replayComplete' })
      emit({ type: 'user', turnId: 'u1', text: 'find the widget' })
      emit({ type: 'turnStart', turnId: 'a1' })
      emit({ type: 'textDelta', turnId: 'a1', text: 'Found the widget.' })
      emit({ type: 'turnEnd', turnId: 'a1' })
    })
    return view
  }

  it('with nothing focused (document.body)', async () => {
    await renderTranscript()
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)
    expect(find(document.body).defaultPrevented).toBe(true)
    expect(await screen.findByRole('searchbox', { name: 'Search this transcript' })).toHaveFocus()
  })

  it('from the session composer, on ⌘F as well as Ctrl+F', async () => {
    const user = userEvent.setup()
    await renderTranscript()
    await user.click(composer())
    expect(find(composer(), 'metaKey').defaultPrevented).toBe(true)
    expect(await screen.findByRole('searchbox', { name: 'Search this transcript' })).toBeInTheDocument()
  })

  it('from the transcript — a click focuses its scroller', async () => {
    const user = userEvent.setup()
    await renderTranscript()
    const scroller = screen.getByTestId('transcript-scroll')
    await user.click(scroller)
    expect(scroller).toHaveFocus()
    expect(find(scroller).defaultPrevented).toBe(true)
    expect(await screen.findByRole('searchbox', { name: 'Search this transcript' })).toBeInTheDocument()
  })

  it('⌘F while the bar is open reselects its query', async () => {
    const user = userEvent.setup()
    await renderTranscript()
    find(document.body)
    const input = await screen.findByRole('searchbox', { name: 'Search this transcript' }) as HTMLInputElement
    await user.type(input, 'widget')
    await user.click(composer())
    expect(find(composer()).defaultPrevented).toBe(true)
    expect(input).toHaveFocus()
    expect([input.selectionStart, input.selectionEnd]).toEqual([0, 6])
    // …and from inside the bar's own input too.
    expect(find(input).defaultPrevented).toBe(true)
  })

  it('Esc closes the bar and returns focus to where ⌘F was pressed — without detaching', async () => {
    const user = userEvent.setup()
    const app = await renderTranscript()
    await user.click(composer())
    find(composer())
    const input = await screen.findByRole('searchbox', { name: 'Search this transcript' })
    await user.keyboard('{Escape}')
    expect(input).not.toBeInTheDocument()
    expect(composer()).toHaveFocus()
    expect(app.path()).toBe('/sessions/s1')
  })

  it('NOT from another text entry (the list search) — the browser finds instead', async () => {
    const user = userEvent.setup()
    await renderTranscript()
    const search = screen.getByRole('searchbox', { name: /search sessions/i })
    await user.click(search)
    expect(find(search).defaultPrevented).toBe(false)
    expect(searchBar()).toBeNull()
  })

  it('NOT from the chrome (a focused nav link)', async () => {
    await renderTranscript()
    railLink().focus()
    expect(find(railLink()).defaultPrevented).toBe(false)
    expect(searchBar()).toBeNull()
  })

  it('NOT on the Files tab — no transcript is on screen', async () => {
    SESSIONS.push(session({ id: 's3', name: 'Three', cwd: '/work/three' }))
    try {
      await renderTranscript('/sessions/s3?view=files')
      await waitFor(() => expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true'))
      ;(document.activeElement as HTMLElement | null)?.blur()
      expect(find(document.body).defaultPrevented).toBe(false)
      expect(searchBar()).toBeNull()
    } finally {
      SESSIONS.pop()
    }
  })

  it('NOT in Settings — no session is open', async () => {
    renderApp({ path: '/settings', mode: 'desktop' })
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(find(document.body).defaultPrevented).toBe(false)
  })

  it('NOT with no session open', () => {
    renderApp({ path: '/sessions', mode: 'desktop' })
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(find(document.body).defaultPrevented).toBe(false)
  })
})
