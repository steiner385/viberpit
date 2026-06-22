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

  it('two-stage Escape: first Escape stops propagation and blurs the canvas', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)

    // The canvas container is the focusable div (tabIndex={0})
    const canvas = document.querySelector('[tabIndex="0"]') as HTMLElement
    expect(canvas).not.toBeNull()

    // Focus the canvas
    canvas.focus()
    expect(document.activeElement).toBe(canvas)

    // Set up a parent listener to verify the event does NOT bubble
    const parentListener = vi.fn()
    document.addEventListener('keydown', parentListener)

    try {
      // Simulate Escape keydown on the canvas
      const escapeEvent = new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      })
      const stopPropSpy = vi.spyOn(escapeEvent, 'stopPropagation')

      canvas.dispatchEvent(escapeEvent)

      // stopPropagation must have been called
      expect(stopPropSpy).toHaveBeenCalled()
      // Canvas must no longer be focused (blurred)
      expect(document.activeElement).not.toBe(canvas)
      // Parent listener must NOT have been called (event was stopped)
      expect(parentListener).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', parentListener)
    }
  })
})
