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
})
