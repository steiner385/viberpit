import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { BrowserTab } from '../BrowserTab'

vi.mock('../../vnc/vncClient', () => ({
  VncClient: class {
    on() {}
    connect() {}
    disconnect() {}
    setScale() {}
    focus() {}
    blur() {}
  },
}))

describe('BrowserTab', () => {
  it('shows the agent-driving badge when status is working', () => {
    render(<BrowserTab sessionId="S" statusState="working" />)
    expect(screen.getByText(/agent is driving/i)).toBeInTheDocument()
  })

  it('shows the disabled copy on a disabled state', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="disabled" />)
    expect(screen.getByText(/isn.t enabled on this server/i)).toBeInTheDocument()
  })

  it('shows empty-state copy with an open-tab action when connected but blank', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.getByText(/hasn.t opened a browser yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /open a browser/i })).toBeInTheDocument()
  })
})
