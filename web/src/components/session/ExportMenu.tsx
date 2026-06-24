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
          'inline-flex items-center px-2 py-0.5 rounded-md min-h-[28px] text-xs',
          'hover:bg-gray-200 dark:hover:bg-gray-700 active:bg-gray-300 dark:active:bg-gray-600',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'text-gray-600 dark:text-gray-400',
        )}
      >
        Export
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Export transcript"
          className={cn(
            'absolute right-0 top-full z-10 mt-1 w-36',
            'rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg py-1',
          )}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => pick('md')}
            className={cn(
              'w-full text-left px-3 py-1.5 text-xs',
              'hover:bg-gray-100 dark:hover:bg-gray-800',
              'text-gray-700 dark:text-gray-300',
              'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
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
              'hover:bg-gray-100 dark:hover:bg-gray-800',
              'text-gray-700 dark:text-gray-300',
              'focus-visible:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-gray-800',
            )}
          >
            Raw JSONL
          </button>
        </div>
      )}
    </div>
  )
}
