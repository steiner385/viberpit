import { useState } from 'react'
import { cn } from '@/lib/utils'
import { buildLines } from '@/lib/toolContent'

// `input` is the tool's call arguments (Edit: old_string/new_string, Write:
// content). The result payload carries only the success/error message, so the
// diff MUST be reconstructed from the input.
interface Props { input: Record<string, unknown> }

const MAX_LINES = 200

export function DiffView({ input }: Props) {
  const [showAll, setShowAll] = useState(false)
  const allLines = buildLines(input)
  const visible = showAll ? allLines : allLines.slice(0, MAX_LINES)
  const hidden = allLines.length - visible.length

  return (
    <div className="max-h-64 overflow-auto rounded bg-gray-50 dark:bg-gray-900 text-xs font-mono md:max-h-96">
      <pre className="p-2">
        {visible.map((l, i) => (
          <div
            key={i}
            className={cn(
              l.prefix === '-'
                ? 'text-red-700 dark:text-red-400'
                : 'text-green-700 dark:text-green-400',
            )}
          >
            {l.prefix} {l.text}
          </div>
        ))}
      </pre>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="px-3 py-1 text-xs text-blue-600 dark:text-blue-400 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
        >
          show all {allLines.length} lines
        </button>
      )}
    </div>
  )
}
