import { useState } from 'react'
import { cn } from '@/lib/utils'
import { stripAnsi, coerceText, splitImages } from '@/lib/toolContent'
import { ResultImages } from './ResultImages'

interface Props { payload: Record<string, unknown>; ok?: boolean }

const MAX_LINES = 300

export function FileContent({ payload, ok = true }: Props) {
  const [showAll, setShowAll] = useState(false)
  // Image blocks (Read of a PNG) render as thumbnails; the remaining `content`
  // may be a string OR a list of content blocks — normalize both.
  const { images, rest } = splitImages(payload.content)
  const raw = payload.content != null ? coerceText(rest) : JSON.stringify(payload, null, 2)
  const cleaned = stripAnsi(raw)
  const allLines = cleaned.split('\n')
  const visible = showAll ? allLines : allLines.slice(0, MAX_LINES)
  const hidden = allLines.length - visible.length
  // A pure-image result has no text pane; don't render an empty <pre>.
  const hasText = payload.content == null || cleaned.trim() !== ''

  return (
    <div>
      {hasText && (
      <pre
        className={cn(
          'max-h-64 overflow-auto rounded p-2 text-xs font-mono md:max-h-96',
          ok
            ? 'bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200'
            : 'bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-300',
        )}
      >
        {visible.join('\n')}
      </pre>
      )}
      <ResultImages images={images} />
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="mt-1 text-xs text-blue-600 dark:text-blue-400 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
        >
          show {hidden} more lines
        </button>
      )}
    </div>
  )
}
