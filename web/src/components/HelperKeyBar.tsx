/**
 * HelperKeyBar — on-screen key bar for mobile/touch users in a terminal session.
 *
 * Renders below the terminal with ≥44px touch targets for common control keys.
 * Ctrl acts as a modifier toggle: when active, the next programmatic key press
 * (or explicit Ctrl+<letter> button, if added) composes the correct byte.
 *
 * Design note: this component is intentionally stateless w.r.t. the socket.
 * The parent wires `onKey` to `socket.input(sessionId, data)`. This keeps the
 * component testable without a real WebSocket.
 */

import { useState } from 'react'
import { cn } from '@/lib/utils'

export interface HelperKeyBarProps {
  /** Called with the raw byte sequence to send to the PTY. */
  onKey: (data: string) => void
  className?: string
}

interface KeyDef {
  label: string
  ariaLabel: string
  data: string
  wide?: boolean
}

const ARROW_KEYS: KeyDef[] = [
  { label: '↑', ariaLabel: 'Arrow Up',    data: '\x1b[A' },
  { label: '↓', ariaLabel: 'Arrow Down',  data: '\x1b[B' },
  { label: '←', ariaLabel: 'Arrow Left',  data: '\x1b[D' },
  { label: '→', ariaLabel: 'Arrow Right', data: '\x1b[C' },
]

const OTHER_KEYS: KeyDef[] = [
  { label: 'Esc', ariaLabel: 'Escape', data: '\x1b' },
  { label: 'Tab', ariaLabel: 'Tab',    data: '\t' },
]

/**
 * Common Ctrl+letter sequences the user can trigger explicitly.
 * The label shows Ctrl+X; the data is the control byte.
 */
const CTRL_COMBOS: { label: string; ariaLabel: string; data: string }[] = [
  { label: 'C', ariaLabel: 'Ctrl+C (interrupt)', data: '\x03' },
  { label: 'D', ariaLabel: 'Ctrl+D (EOF)',        data: '\x04' },
  { label: 'Z', ariaLabel: 'Ctrl+Z (suspend)',    data: '\x1a' },
]

const BTN = cn(
  'flex items-center justify-center rounded-md font-mono text-sm font-medium',
  'min-h-[44px] min-w-[44px] px-3',
  'bg-gray-800 text-gray-100 dark:bg-gray-900 dark:text-gray-100',
  'border border-gray-600 dark:border-gray-700',
  'active:bg-gray-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
  'select-none touch-manipulation',
)

const BTN_ACTIVE = cn('bg-blue-700 border-blue-500 text-white')

export function HelperKeyBar({ onKey, className }: HelperKeyBarProps) {
  const [ctrlActive, setCtrlActive] = useState(false)

  function sendKey(data: string) {
    onKey(data)
    setCtrlActive(false)
  }

  function handleCtrlToggle() {
    setCtrlActive((v) => !v)
  }

  return (
    <div
      role="toolbar"
      aria-label="Helper keys"
      className={cn(
        'flex flex-wrap items-center gap-1.5 p-2',
        'bg-gray-900/95 border-t border-gray-700',
        className,
      )}
    >
      {/* Static keys */}
      {OTHER_KEYS.map((k) => (
        <button
          key={k.ariaLabel}
          type="button"
          aria-label={k.ariaLabel}
          className={BTN}
          onPointerDown={(e) => { e.preventDefault(); sendKey(k.data) }}
        >
          {k.label}
        </button>
      ))}

      {/* Ctrl modifier toggle */}
      <button
        type="button"
        aria-label="Ctrl modifier toggle"
        aria-pressed={ctrlActive}
        className={cn(BTN, ctrlActive && BTN_ACTIVE)}
        onPointerDown={(e) => { e.preventDefault(); handleCtrlToggle() }}
      >
        Ctrl
      </button>

      {/* Ctrl combo buttons — only visible when Ctrl is active */}
      {ctrlActive && CTRL_COMBOS.map((k) => (
        <button
          key={k.ariaLabel}
          type="button"
          aria-label={k.ariaLabel}
          className={cn(BTN, 'bg-blue-800 border-blue-500 text-white')}
          onPointerDown={(e) => { e.preventDefault(); sendKey(k.data) }}
        >
          {k.label}
        </button>
      ))}

      {/* Separator */}
      <div className="w-px h-8 bg-gray-700 mx-0.5" aria-hidden="true" />

      {/* Arrow keys */}
      {ARROW_KEYS.map((k) => (
        <button
          key={k.ariaLabel}
          type="button"
          aria-label={k.ariaLabel}
          className={BTN}
          onPointerDown={(e) => { e.preventDefault(); sendKey(k.data) }}
        >
          {k.label}
        </button>
      ))}
    </div>
  )
}
