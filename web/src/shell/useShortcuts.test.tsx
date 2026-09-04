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
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Session } from '@/api/types'

// ── the fake daemon (same shape as AppShell.test.tsx: the CLIENT, not the hooks) ──

function session(over: Partial<Session> = {}): Session {
  return {
    id: 's1',
    name: 'One',
    accountName: 'alice',
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

  it('NOT while a modal dialog is open', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions?dialog=new', mode: 'desktop' })
    await waitFor(() => expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument())
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

  it('NOT while a modal dialog is open', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions?dialog=new', mode: 'desktop' })
    await waitFor(() => expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument())
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
