import { useState } from 'react'
import { cn } from '@/lib/utils'

interface Props { payload: Record<string, unknown> }

const MAX_LINES = 200

function buildLines(payload: Record<string, unknown>): Array<{ prefix: '+' | '-'; text: string }> {
  const lines: Array<{ prefix: '+' | '-'; text: string }> = []
  const oldStr = typeof payload.old_string === 'string' ? payload.old_string : null
  const newStr = typeof payload.new_string === 'string' ? payload.new_string : null
  const content = typeof payload.content === 'string' ? payload.content : null

  if (oldStr !== null) {
    for (const line of oldStr.split('\n')) {
      lines.push({ prefix: '-', text: line })
    }
  }
  if (newStr !== null) {
    for (const line of newStr.split('\n')) {
      lines.push({ prefix: '+', text: line })
    }
  } else if (content !== null && oldStr === null) {
    // Write-only: content as added lines
    for (const line of content.split('\n')) {
      lines.push({ prefix: '+', text: line })
    }
  }
  return lines
}

export function DiffView({ payload }: Props) {
  const [showAll, setShowAll] = useState(false)
  const allLines = buildLines(payload)
  const visible = showAll ? allLines : allLines.slice(0, MAX_LINES)
  const hidden = allLines.length - visible.length

  return (
    <div className="overflow-auto rounded bg-gray-50 dark:bg-gray-900 text-xs font-mono">
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
