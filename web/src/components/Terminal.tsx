/**
 * Terminal — full-screen interactive terminal view for a ccswitch session.
 *
 * Accessibility partial-conformance note (R6, WCAG 2.1 AA):
 *   Management surfaces (list, accounts, controls, dialogs) meet WCAG 2.1 AA in full.
 *   The live PTY stream rendered inside <div id="xterm-container"> cannot fully satisfy AA
 *   because it is an "essential live process" under WCAG SC 1.1.1 and SC 2.2.2 — a raw
 *   character stream at variable cadence cannot be made accessible without breaking its
 *   utility (a running terminal program is inherently non-static). Partial conformance
 *   is provided via:
 *     - xterm.js `screenReaderMode: true` (outputs an ARIA-live screen-reader buffer).
 *     - An additional `aria-live="polite"` region below mirroring the last ~20 lines of
 *       output (debounced 200ms) as plain text for screen readers.
 *     - A visible "← Back" button that is keyboard-reachable and returns focus to the
 *       originating list item.
 *     - Session status conveyed by text, not color alone.
 */

import { useEffect, useRef, useState, useCallback } from 'react'
import { cn } from '@/lib/utils'
import { HelperKeyBar } from './HelperKeyBar'
import { useSessionSocket } from '@/ws/client'
import type { WsOutputMsg, WsSessionEndedMsg } from '@/ws/client'

interface TerminalProps {
  sessionId: string
  onBack: () => void
}

// Number of recent output lines to mirror into the aria-live region.
const ARIA_LIVE_LINE_LIMIT = 20
// Debounce delay (ms) for updating the aria-live mirror.
const ARIA_LIVE_DEBOUNCE_MS = 200

export function Terminal({ sessionId, onBack }: TerminalProps) {
  const containerRef  = useRef<HTMLDivElement>(null)
  const xtermRef      = useRef<import('@xterm/xterm').Terminal | null>(null)
  const fitRef        = useRef<import('@xterm/addon-fit').FitAddon | null>(null)
  const [ended, setEnded]       = useState(false)
  const [endedCode, setEndedCode] = useState<number | null>(null)
  const outputLinesRef = useRef<string[]>([])
  const [ariaLiveText, setAriaLiveText] = useState('')
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const { attach, detach, input, resize, on, off, connectionState } = useSessionSocket()

  // Debounced aria-live updater
  const flushAriaLive = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      const lines = outputLinesRef.current
      const recent = lines.slice(-ARIA_LIVE_LINE_LIMIT).join('\n')
      setAriaLiveText(recent)
    }, ARIA_LIVE_DEBOUNCE_MS)
  }, [])

  // Mount xterm
  useEffect(() => {
    let mounted = true

    // Dynamic import avoids xterm in jsdom/test environments
    Promise.all([
      import('@xterm/xterm'),
      import('@xterm/addon-fit'),
    ]).then(([{ Terminal: XTerm }, { FitAddon }]) => {
      if (!mounted || !containerRef.current) return

      const term = new XTerm({
        screenReaderMode: true,
        convertEol: true,
        cursorBlink: true,
        fontSize: 14,
        fontFamily: 'Menlo, Consolas, "Courier New", monospace',
        theme: { background: '#0d0d0d', foreground: '#d4d4d4' },
        allowProposedApi: true,
      })
      const fit = new FitAddon()
      term.loadAddon(fit)
      term.open(containerRef.current)
      fit.fit()
      term.focus()

      xtermRef.current = term
      fitRef.current   = fit

      // Send keystrokes to the PTY
      term.onData((data) => {
        input(sessionId, data)
      })
    })

    return () => {
      mounted = false
      xtermRef.current?.dispose()
      xtermRef.current = null
      fitRef.current   = null
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // Attach / detach on mount / unmount
  useEffect(() => {
    attach(sessionId)
    return () => { detach(sessionId) }
  }, [sessionId, attach, detach])

  // Handle incoming output
  useEffect(() => {
    const handler = (msg: WsOutputMsg) => {
      if (msg.sessionId !== sessionId) return
      // The server base64-encodes raw PTY bytes (JSON can't carry binary). Decode
      // to bytes and write those to xterm; decode to text for the aria-live mirror.
      const bytes = Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0))
      xtermRef.current?.write(bytes)
      const text = new TextDecoder().decode(bytes)
      const plain = text.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\r/g, '')
      const newLines = plain.split('\n')
      outputLinesRef.current = [...outputLinesRef.current, ...newLines].slice(-500)
      flushAriaLive()
    }
    on('output', handler)
    return () => off('output', handler)
  }, [sessionId, on, off, flushAriaLive])

  // Handle session ended
  useEffect(() => {
    const handler = (msg: WsSessionEndedMsg) => {
      if (msg.sessionId !== sessionId) return
      setEnded(true)
      setEndedCode(msg.code)
    }
    on('sessionEnded', handler)
    return () => off('sessionEnded', handler)
  }, [sessionId, on, off])

  // ResizeObserver → fit + resize message
  useEffect(() => {
    if (!containerRef.current) return
    const observer = new ResizeObserver(() => {
      if (!fitRef.current || !xtermRef.current) return
      fitRef.current.fit()
      const { cols, rows } = xtermRef.current
      resize(sessionId, cols, rows)
    })
    observer.observe(containerRef.current)
    return () => observer.disconnect()
  }, [sessionId, resize])

  function handleBack() {
    detach(sessionId)
    onBack()
  }

  function handleHelperKey(data: string) {
    input(sessionId, data)
  }

  return (
    <div className="flex flex-col h-screen bg-[#0d0d0d]">
      {/* Top bar */}
      <div className={cn(
        'flex items-center gap-3 px-3 py-2',
        'bg-gray-900 border-b border-gray-700',
        'flex-shrink-0',
      )}>
        <button
          type="button"
          onClick={handleBack}
          className={cn(
            'flex items-center gap-1.5 px-3 py-2 rounded-md min-h-[44px]',
            'text-sm font-medium text-gray-200',
            'hover:bg-gray-700 active:bg-gray-600',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
          aria-label="Back to session list"
        >
          ← Back
        </button>

        <span className="text-xs text-gray-400 ml-auto">
          {connectionState !== 'connected' && (
            <span aria-live="polite" aria-atomic="true">
              {connectionState === 'reconnecting' ? 'Reconnecting…' : 'Disconnected'}
            </span>
          )}
        </span>
      </div>

      {/* xterm container */}
      <div className="relative flex-1 overflow-hidden">
        <div
          ref={containerRef}
          tabIndex={0}
          aria-label="Terminal"
          className="w-full h-full"
        />

        {/* Ended overlay */}
        {ended && (
          <div
            className={cn(
              'absolute inset-0 flex flex-col items-center justify-center',
              'bg-black/70 backdrop-blur-sm',
            )}
            role="alert"
            aria-live="assertive"
          >
            <p className="text-white text-lg font-semibold mb-1">Session ended</p>
            {endedCode !== null && (
              <p className="text-gray-400 text-sm mb-4">Exit code: {endedCode}</p>
            )}
            <button
              type="button"
              onClick={handleBack}
              className={cn(
                'px-4 py-2 rounded-md min-h-[44px]',
                'bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              ← Back to list
            </button>
          </div>
        )}
      </div>

      {/*
       * aria-live recent-output mirror (SR-only). Mirrors the last ARIA_LIVE_LINE_LIMIT lines of
       * PTY output as plain text so screen readers get a readable summary of recent activity.
       * ANSI escape sequences are stripped; the mirror is debounced to avoid flooding the AT.
       *
       * Partial-conformance note: this does NOT provide a full transcript — it is a live window.
       * The full terminal contents are accessible via xterm's own screenReaderMode buffer.
       */}
      <div
        aria-live="polite"
        aria-atomic="false"
        aria-label="Recent terminal output"
        className={cn(
          'sr-only',
          // Visible for debugging: uncomment the line below to see the mirror on-screen
          // 'absolute bottom-0 left-0 right-0 h-24 bg-black/80 text-white text-xs p-2 overflow-hidden font-mono',
        )}
      >
        {ariaLiveText}
      </div>

      {/* Helper key bar (mobile) */}
      <HelperKeyBar onKey={handleHelperKey} className="flex-shrink-0" />
    </div>
  )
}
