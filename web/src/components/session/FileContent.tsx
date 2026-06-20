import { useState } from 'react'

interface Props { payload: Record<string, unknown> }

const MAX_LINES = 300

function stripAnsi(s: string): string {
  return s.replace(/\x1b\[[0-9;]*m/g, '')
}

export function FileContent({ payload }: Props) {
  const [showAll, setShowAll] = useState(false)
  const raw = typeof payload.content === 'string' ? payload.content : JSON.stringify(payload, null, 2)
  const cleaned = stripAnsi(raw)
  const allLines = cleaned.split('\n')
  const visible = showAll ? allLines : allLines.slice(0, MAX_LINES)
  const hidden = allLines.length - visible.length

  return (
    <div>
      <pre className="overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
        {visible.join('\n')}
      </pre>
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
