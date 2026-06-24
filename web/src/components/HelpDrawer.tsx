import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { shortcutsByGroup, type Shortcut } from '@/lib/shortcuts'

const NAV_MAP = [
  { name: 'Sessions', detail: 'Live sessions list — the default view' },
  { name: 'Accounts', detail: 'Manage Claude accounts and rotation' },
  { name: 'Archived', detail: 'Review, unarchive, or permanently delete past sessions' },
  { name: 'Session view', detail: 'Open a session to read its transcript and send prompts' },
  { name: 'Review', detail: 'Read-only transcript of an archived session' },
]

const TIPS = [
  {
    title: 'List-focus mode',
    detail:
      'Press Tab on an empty prompt to focus the list; then c/r/a/d act on the row. Esc returns to the prompt.',
  },
  {
    title: 'Accounts = config dirs',
    detail:
      'Each account is an isolated CLAUDE_CONFIG_DIR; ccswitch rotates them as usage allows.',
  },
  {
    title: 'Archive vs delete',
    detail: 'Archive (a) hides a session but keeps it; delete (d) removes it permanently.',
  },
  {
    title: 'Permission modes',
    detail: 'Inside a session, Shift+Tab cycles the permission mode.',
  },
]

export function HelpDrawer({ onClose }: { onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    closeButtonRef.current?.focus()
  }, [])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key === 'Tab') {
        const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
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
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      data-testid="help-drawer-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-drawer-title"
        className={cn(
          'h-full w-full max-w-md overflow-y-auto p-6 space-y-6',
          'bg-white dark:bg-gray-800 border-l border-gray-200 dark:border-gray-600 shadow-xl',
        )}
      >
        <div className="flex items-center justify-between">
          <h2
            id="help-drawer-title"
            className="text-lg font-semibold text-gray-900 dark:text-gray-100"
          >
            Help &amp; Shortcuts
          </h2>
          <button
            ref={closeButtonRef}
            type="button"
            aria-label="Close help"
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

        <ShortcutSection title="Compose mode" bindings={shortcutsByGroup('compose')} />
        <ShortcutSection title="List-focus mode" bindings={shortcutsByGroup('list-mode')} />
        <ShortcutSection title="In-session" bindings={shortcutsByGroup('in-session')} />

        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
            Navigation
          </h3>
          <dl className="space-y-1">
            {NAV_MAP.map((n) => (
              <div key={n.name} className="flex items-baseline gap-3">
                <dt className="shrink-0 min-w-[110px] text-sm font-medium text-gray-700 dark:text-gray-300">
                  {n.name}
                </dt>
                <dd className="text-sm text-gray-600 dark:text-gray-400">{n.detail}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
            Tips
          </h3>
          <dl className="space-y-2">
            {TIPS.map((t) => (
              <div key={t.title}>
                <dt className="text-sm font-medium text-gray-700 dark:text-gray-300">{t.title}</dt>
                <dd className="text-sm text-gray-600 dark:text-gray-400">{t.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </div>
  )
}

function ShortcutSection({ title, bindings }: { title: string; bindings: Shortcut[] }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">
        {title}
      </h3>
      <dl className="space-y-1">
        {bindings.map((b) => (
          <div key={b.id} className="flex items-baseline gap-3">
            <dt className="shrink-0 min-w-[120px]">
              <kbd
                className={cn(
                  'inline-block rounded px-1.5 py-0.5 text-xs font-mono',
                  'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300',
                  'border border-gray-300 dark:border-gray-600',
                )}
              >
                {b.keys}
              </kbd>
            </dt>
            <dd className="text-sm text-gray-700 dark:text-gray-300">{b.description}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
