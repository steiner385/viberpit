import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { BrowserTab } from './BrowserTab'

function setCoarsePointer(coarse: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('coarse') ? coarse : false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  }))
}

afterEach(() => vi.unstubAllGlobals())

describe('BrowserTab keyboard toggle', () => {
  it('hides the toggle on fine-pointer (desktop) devices', () => {
    setCoarsePointer(false)
    render(<BrowserTab sessionId="s" statusState="idle" initialState="connected" />)
    expect(screen.queryByRole('button', { name: /keyboard/i })).toBeNull()
  })

  it('shows the toggle on coarse-pointer (touch) devices and reflects aria-pressed', () => {
    setCoarsePointer(true)
    render(<BrowserTab sessionId="s" statusState="idle" initialState="connected" />)
    const btn = screen.getByRole('button', { name: /keyboard/i })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('relays typed text from the hidden input through to the session', () => {
    setCoarsePointer(true)
    render(<BrowserTab sessionId="s" statusState="idle" initialState="connected" />)
    fireEvent.click(screen.getByRole('button', { name: /keyboard/i }))
    const proxy = screen.getByLabelText('Keyboard input') as HTMLInputElement
    // With initialState injected, clientRef is null, so sendKey path must not throw.
    proxy.value = 'a'
    expect(() => fireEvent.input(proxy)).not.toThrow()
    // Proxy is cleared after each input so it never accumulates / composes.
    expect(proxy.value).toBe('')
  })
})
