import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

interface ShortcutsHelpProps {
  onClose: () => void
}

interface Binding {
  keys: string
  description: string
}

const NAVIGATION_BINDINGS: Binding[] = [
  { keys: '↑ / ↓', description: 'Navigate sessions (at line boundary)' },
  { keys: '→ / Enter (empty)', description: 'Open highlighted session' },
  { keys: 'Esc', description: 'Clear prompt and deselect' },
  { keys: 'Alt+1–9', description: 'Jump to session by number' },
]

const ACTION_BINDINGS: Binding[] = [
  { keys: 'r / F2', description: 'Rename highlighted session' },
  { keys: 'Delete', description: 'Close highlighted session' },
  { keys: 'Enter (with text)', description: 'Create new session with prompt' },
  { keys: 'Shift+Enter', description: 'Insert newline in prompt' },
  { keys: '?', description: 'Show this shortcuts overlay' },
]

const IN_SESSION_BINDINGS: Binding[] = [
  { keys: 'Esc', description: 'Back to session list' },
  { keys: 'Shift+Tab', description: 'Cycle permission mode' },
  { keys: 'c', description: 'Collapse / expand tool calls' },
]

export function ShortcutsHelp({ onClose }: ShortcutsHelpProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  // Focus the close button on mount
  useEffect(() => {
    closeButtonRef.current?.focus()
  }, [])

  // Escape to close + Tab focus trap
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault()
            last.focus()
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault()
            first.focus()
          }
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="shortcuts-help-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-md rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-5',
          'max-h-[90vh] overflow-y-auto',
        )}
      >
        <div className="flex items-center justify-between">
          <h2
            id="shortcuts-help-title"
            className="text-lg font-semibold text-gray-900 dark:text-gray-100"
          >
            Keyboard Shortcuts
          </h2>
          <button
            ref={closeButtonRef}
            type="button"
            aria-label="Close shortcuts help"
            onClick={onClose}
            className={cn(
              'h-8 w-8 flex items-center justify-center rounded-md',
              'text-gray-500 dark:text-gray-400',
              'hover:bg-gray-100 dark:hover:bg-gray-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            )}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <ShortcutGroup title="Navigation" bindings={NAVIGATION_BINDINGS} />
        <ShortcutGroup title="Actions" bindings={ACTION_BINDINGS} />
        <ShortcutGroup title="In-session" bindings={IN_SESSION_BINDINGS} />
      </div>
    </div>
  )
}

function ShortcutGroup({ title, bindings }: { title: string; bindings: Binding[] }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
        {title}
      </h3>
      <dl className="space-y-1">
        {bindings.map(({ keys, description }) => (
          <div key={keys} className="flex items-baseline gap-3">
            <dt className="shrink-0 min-w-[100px]">
              <kbd className={cn(
                'inline-block rounded px-1.5 py-0.5 text-xs font-mono',
                'bg-gray-100 dark:bg-gray-700',
                'text-gray-700 dark:text-gray-300',
                'border border-gray-300 dark:border-gray-600',
              )}>
                {keys}
              </kbd>
            </dt>
            <dd className="text-sm text-gray-700 dark:text-gray-300">{description}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
