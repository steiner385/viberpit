import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

// BrowserTab opens a real WebSocket; stub it so the panel renders in jsdom.
vi.mock('@/features/sessions/browser/BrowserTab', () => ({
  BrowserTab: ({ sessionId }: { sessionId: string }) => <div data-testid="browser-tab">{sessionId}</div>,
}))

import { ReauthBrowserPanel } from './ReauthBrowserPanel'

describe('ReauthBrowserPanel', () => {
  // The viewer is loaded lazily (#1411) — a VNC client is ~65kB gzip that no page
  // load needs until someone actually opens it — so it arrives one microtask in.
  it('renders a BrowserTab pointed at the given vnc session key', async () => {
    render(<ReauthBrowserPanel vncSession="__reauth__" />)
    expect(await screen.findByTestId('browser-tab')).toHaveTextContent('__reauth__')
  })

  it('wraps the viewer in a large, user-resizable container so the login is usable', async () => {
    render(<ReauthBrowserPanel vncSession="__reauth__" />)
    // The viewer must be resizable (drag handle) and tall by default, not a thumbnail.
    const box = (await screen.findByTestId('browser-tab')).parentElement as HTMLElement
    expect(box.className).toContain('resize')
    expect(box.className).toMatch(/h-\[\d+vh\]/)
  })
})
