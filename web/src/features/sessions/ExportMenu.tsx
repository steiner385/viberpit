/**
 * ExportMenu — an "Export" button that opens a two-item dropdown menu
 * (Markdown / Raw JSONL) and triggers a transcript download on pick.
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { downloadTranscript } from '@/api/transcript'

interface Props {
  sessionId: string
}

export function ExportMenu({ sessionId }: Props) {
  const [open, setOpen] = useState(false)

  function pick(format: 'md' | 'jsonl') {
    setOpen(false)
    downloadTranscript(sessionId, format)
  }

  return (
    <div className="relative">
      <button
        type="button"
        aria-label="Export transcript"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'inline-flex items-center px-2 py-0.5 rounded-md min-h-7 text-xs',
          'hover:bg-edge active:bg-edge',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          'text-ink-muted',
        )}
      >
        Export
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Export transcript"
          className={cn(
            'absolute right-0 top-full z-sticky mt-1 w-36',
            'rounded-md border border-edge bg-surface shadow-lg py-1',
          )}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => pick('md')}
            className={cn(
              'w-full text-left px-3 py-1.5 text-xs',
              'hover:bg-surface-raised',
              'text-ink',
              'focus-visible:outline-none focus-visible:bg-surface-raised',
            )}
          >
            Markdown
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => pick('jsonl')}
            className={cn(
              'w-full text-left px-3 py-1.5 text-xs',
              'hover:bg-surface-raised',
              'text-ink',
              'focus-visible:outline-none focus-visible:bg-surface-raised',
            )}
          >
            Raw JSONL
          </button>
        </div>
      )}
    </div>
  )
}
