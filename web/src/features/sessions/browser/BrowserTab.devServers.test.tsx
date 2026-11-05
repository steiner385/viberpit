import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render as rtlRender, screen, fireEvent, act } from '@testing-library/react'
import type { ReactElement } from 'react'
import { BrowserTab } from './BrowserTab'
import { ViewportProvider } from '@/shell/useViewport'
import type { VncState } from '@/vnc/vncClient'

function render(ui: ReactElement) {
  return rtlRender(<ViewportProvider kiosk={null}>{ui}</ViewportProvider>)
}

/** Every VncClient the tab builds, with the listeners it registered. */
const clients: { url: string; listeners: Record<string, ((s: VncState) => void)[]>; disconnected: boolean }[] = []

vi.mock('@/vnc/vncClient', () => ({
  VncClient: class {
    rec: (typeof clients)[number]
    constructor(url: string) {
      this.rec = { url, listeners: {}, disconnected: false }
      clients.push(this.rec)
    }
    on(ev: string, cb: (s: VncState) => void) { (this.rec.listeners[ev] ||= []).push(cb) }
    connect() {}
    disconnect() { this.rec.disconnected = true }
    refit() {}
    focus() {}
    blur() {}
  },
}))

// The section itself is covered by DevServers.test.tsx; here it is a seam that
// reports what the tab handed it and lets the test land an Open.
vi.mock('./DevServers', () => ({
  DevServers: ({ notice, onPreviewOpened, fill }: { notice: string | null; onPreviewOpened: () => void; fill?: boolean }) => (
    <section aria-label="Dev servers" data-fill={fill ? 'yes' : 'no'}>
      {notice && <p role="status">{notice}</p>}
      <button type="button" onClick={onPreviewOpened}>land an open</button>
    </section>
  ),
}))

/** SessionBody's list, as the tab receives it. */
const LIST = { servers: [], refetch: () => {} }

const emit = (ev: string, s: VncState) => act(() => clients.at(-1)!.listeners[ev]?.forEach((cb) => cb(s)))

beforeEach(() => { clients.length = 0 })

describe('BrowserTab dev servers (D-18)', () => {
  it('shows no section without a list: the reauth panel, or detection off', () => {
    const { unmount } = render(<BrowserTab sessionId="S" statusState="idle" />)
    expect(screen.queryByRole('region', { name: 'Dev servers' })).toBeNull()
    unmount()
    render(<BrowserTab sessionId="S" statusState="idle" devServers={{ servers: null, refetch: () => {} }} />)
    expect(screen.queryByRole('region', { name: 'Dev servers' })).toBeNull()
  })

  it('with X11 off, fills the tab with the list and streams nothing until an Open', () => {
    render(<BrowserTab sessionId="S" statusState="idle" x11Enabled={false} devServers={LIST} />)
    expect(screen.getByRole('region', { name: 'Dev servers' })).toHaveAttribute('data-fill', 'yes')
    expect(screen.queryByLabelText('Live browser view')).toBeNull()
    expect(clients).toHaveLength(0)
  })

  it('a native session switches to the VNC view after an Open, and back with "No preview open" when its display goes away', () => {
    render(<BrowserTab sessionId="S" statusState="idle" displayMode="native" host="dobby" devServers={LIST} />)
    expect(screen.getByText(/opens windows on dobby's desktop/i)).toBeInTheDocument()
    expect(clients).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'land an open' }))
    expect(screen.getByLabelText('Live browser view')).toBeInTheDocument()
    expect(clients).toHaveLength(1)
    expect(clients[0].url).toMatch(/\/ws\/vnc\?session=S$/)

    // The preview display closed or was reaped unwatched: a 4404.
    emit('disconnect', 'nodisplay')
    expect(screen.queryByLabelText('Live browser view')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('No preview open')
    expect(screen.getByText(/opens windows on dobby's desktop/i)).toBeInTheDocument()
    expect(clients[0].disconnected).toBe(true)

    // The next Open streams again and clears the notice.
    fireEvent.click(screen.getByRole('button', { name: 'land an open' }))
    expect(screen.getByLabelText('Live browser view')).toBeInTheDocument()
    expect(screen.queryByRole('status')).toBeNull()
    expect(clients).toHaveLength(2)
  })

  it('a virtual session keeps its viewer and reconnects it when an Open lands on a display it was not showing', () => {
    render(<BrowserTab sessionId="S" statusState="idle" devServers={LIST} />)
    expect(clients).toHaveLength(1)
    emit('disconnect', 'nodisplay')
    // Not preview-only: the usual no-display copy, not "No preview open".
    expect(screen.getByText(/isn't running/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'land an open' }))
    expect(clients).toHaveLength(2)
    expect(clients[0].disconnected).toBe(true)
    expect(screen.getByText('Connecting…')).toBeInTheDocument()
  })

  it('an Open on a connected virtual viewer does not reconnect it', () => {
    render(<BrowserTab sessionId="S" statusState="idle" devServers={LIST} />)
    emit('connect', 'connected')
    fireEvent.click(screen.getByRole('button', { name: 'land an open' }))
    expect(clients).toHaveLength(1)
    // The blank-display hint is for "the agent hasn't opened a browser" — one is open now.
    expect(screen.queryByText(/hasn.t opened a browser yet/i)).toBeNull()
  })
})
