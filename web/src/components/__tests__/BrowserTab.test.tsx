import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { BrowserTab } from '../BrowserTab'

const refitSpy = vi.fn()

vi.mock('../../vnc/vncClient', () => ({
  VncClient: class {
    on() {}
    connect() {}
    disconnect() {}
    refit() { refitSpy() }
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

  it('defaults to Fit and hides the Fit-reset control until zoomed', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.getByText('Fit')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reset to fit/i })).toBeNull()
  })

  it('zoom-in updates the pill to a percentage and reveals Fit-reset', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    fireEvent.click(screen.getByRole('button', { name: /zoom in/i }))
    expect(screen.getByText('110%')).toBeInTheDocument()
    const reset = screen.getByRole('button', { name: /reset to fit/i })
    fireEvent.click(reset)
    expect(screen.getByText('Fit')).toBeInTheDocument()
  })

  it('drives zoom by size alone — no second transform-scale magnification', () => {
    // Regression: the zoom layer must NOT both size itself zoom×100% AND
    // transform: scale(zoom) — noVNC's autoscale (getBoundingClientRect, which
    // includes ancestor transforms) would then magnify a second time, netting
    // ~zoom² on-screen. Net magnification must equal zoom exactly. Asserting the
    // MECHANISM (jsdom has no layout): size reflects zoom, transform carries no
    // scale().
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    const layer = screen.getByTestId('zoom-layer') as HTMLElement

    // Base fit: 100% size, no scale.
    expect(parseFloat(layer.style.width)).toBeCloseTo(100, 5)
    expect(layer.style.transform === '' || layer.style.transform === 'none').toBe(true)

    // Zoom in: size grows to reflect zoom; transform must remain scale-free.
    fireEvent.click(screen.getByRole('button', { name: /zoom in/i }))
    expect(parseFloat(layer.style.width)).toBeCloseTo(110, 5)
    expect(parseFloat(layer.style.height)).toBeCloseTo(110, 5)
    expect(layer.style.transform).not.toContain('scale(')
  })

  it('never renders a "1:1" label', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.queryByText('1:1')).toBeNull()
  })

  it('re-fits the VNC view when the panel resizes', () => {
    vi.useFakeTimers()
    try {
      const cbs: Array<() => void> = []
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ;(globalThis as any).ResizeObserver = class {
        constructor(cb: () => void) { cbs.push(cb) }
        observe() {}
        unobserve() {}
        disconnect() {}
      }
      refitSpy.mockClear()
      // Omit initialState so the VncClient is instantiated (mock handles the connect)
      render(<BrowserTab sessionId="S" statusState="idle" />)
      expect(cbs.length).toBeGreaterThan(0)
      act(() => { cbs[0]() })
      vi.runAllTimers()
      expect(refitSpy).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows at most one bottom caption at a time', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    // "Tap to control" and the empty-state hint must not both be visible.
    const tap = screen.queryByText(/tap to control/i)
    const empty = screen.queryByText(/hasn.t opened a browser yet/i)
    expect([tap, empty].filter(Boolean).length).toBeLessThanOrEqual(1)
  })

  it('labels the live canvas for screen readers', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.getByLabelText(/live browser view/i)).toBeInTheDocument()
  })

  it('hides the empty-state hint after dismiss', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText(/hasn.t opened a browser yet/i)).toBeNull()
  })

  it('on no-display for an idle session, tells the user to send a prompt (not the raw "no display")', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="nodisplay" />)
    expect(screen.getByText(/send it a prompt/i)).toBeInTheDocument()
    expect(screen.queryByText(/no display was allocated/i)).toBeNull()
  })

  it('on no-display while the agent is actively working, shows the generic display fallback', () => {
    // A busy session with no display is the rare pool-exhausted case, not an idle
    // session — "send a prompt" would be wrong advice, so keep a neutral message.
    render(<BrowserTab sessionId="S" statusState="working" initialState="nodisplay" />)
    expect(screen.getByText(/no virtual display is available/i)).toBeInTheDocument()
    expect(screen.queryByText(/send it a prompt/i)).toBeNull()
  })

  it('surfaces a failed "Open a browser" POST instead of swallowing it', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: 'browser cap reached' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
      fireEvent.click(screen.getByRole('button', { name: /open a browser/i }))
      expect(fetchMock).toHaveBeenCalledWith('/api/sessions/S/browser/open', { method: 'POST' })
      await waitFor(() =>
        expect(screen.getByText(/too many browser windows/i)).toBeInTheDocument(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('pastes the client clipboard into the session and confirms', async () => {
    const readText = vi.fn().mockResolvedValue('hunter2')
    vi.stubGlobal('navigator', { clipboard: { readText } })
    try {
      render(<BrowserTab sessionId="S" statusState="working" initialState="connected" />)
      fireEvent.click(screen.getByRole('button', { name: /paste from clipboard/i }))
      expect(readText).toHaveBeenCalled()
      await waitFor(() =>
        expect(screen.getByText(/pasted into the session/i)).toBeInTheDocument(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('surfaces a blocked-clipboard hint instead of failing silently', async () => {
    const readText = vi.fn().mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    vi.stubGlobal('navigator', { clipboard: { readText } })
    try {
      render(<BrowserTab sessionId="S" statusState="working" initialState="connected" />)
      fireEvent.click(screen.getByRole('button', { name: /paste from clipboard/i }))
      await waitFor(() =>
        expect(screen.getByText(/clipboard blocked/i)).toBeInTheDocument(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  afterEach(() => {
    // Reset focus to body between tests to avoid leaking focus state
    if (document.activeElement && document.activeElement !== document.body) {
      (document.activeElement as HTMLElement).blur()
    }
  })
})
