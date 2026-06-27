import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

// BrowserTab opens a real WebSocket; stub it so the panel renders in jsdom.
vi.mock('../BrowserTab', () => ({
  BrowserTab: ({ sessionId }: { sessionId: string }) => <div data-testid="browser-tab">{sessionId}</div>,
}))

import { ReauthBrowserPanel } from '../ReauthBrowserPanel'

describe('ReauthBrowserPanel', () => {
  it('renders a BrowserTab pointed at the given vnc session key', () => {
    render(<ReauthBrowserPanel vncSession="__reauth__" />)
    expect(screen.getByTestId('browser-tab')).toHaveTextContent('__reauth__')
  })

  it('wraps the viewer in a large, user-resizable container so the login is usable', () => {
    render(<ReauthBrowserPanel vncSession="__reauth__" />)
    // The viewer must be resizable (drag handle) and tall by default, not a thumbnail.
    const box = screen.getByTestId('browser-tab').parentElement as HTMLElement
    expect(box.className).toContain('resize')
    expect(box.className).toMatch(/h-\[\d+vh\]/)
  })
})
