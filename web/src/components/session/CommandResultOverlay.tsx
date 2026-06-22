// web/src/components/session/CommandResultOverlay.tsx
import { cn } from '@/lib/utils'
import type { Command } from '@/session/events'

interface Props {
  result: { render: 'help' | 'context'; data: unknown } | null
  commands: Command[]
  onClose: () => void
}

export function CommandResultOverlay({ result, commands, onClose }: Props) {
  if (!result) return null
  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className={cn('w-full max-w-md max-h-[80vh] overflow-y-auto rounded-lg p-4',
          'bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-xl')}
        onClick={(e) => e.stopPropagation()}
      >
        {result.render === 'help' ? (
          <>
            <h2 className="mb-2 text-sm font-semibold">Commands</h2>
            <ul className="space-y-1">
              {commands.map((c) => (
                <li key={c.name} className="flex gap-2 text-sm">
                  <span className="font-mono text-gray-900 dark:text-gray-100">
                    <span aria-hidden="true">/</span>{c.name}
                  </span>
                  <span className="flex-1 truncate text-gray-500 dark:text-gray-400">{c.description}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <h2 className="mb-2 text-sm font-semibold">Context usage</h2>
            <ContextBody data={result.data} />
          </>
        )}
        <button type="button" onClick={onClose}
          className="mt-3 min-h-[44px] rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1 text-sm">
          Close
        </button>
      </div>
    </div>
  )
}

function ContextBody({ data }: { data: unknown }) {
  const d = (data ?? {}) as { percentage?: number; totalTokens?: number; maxTokens?: number }
  const pct = typeof d.percentage === 'number' ? d.percentage.toFixed(1) : '—'
  return (
    <div className="text-sm text-gray-700 dark:text-gray-300">
      <p>{pct}% of context used</p>
      {typeof d.totalTokens === 'number' && typeof d.maxTokens === 'number' && (
        <p className="text-gray-500 dark:text-gray-400">{d.totalTokens.toLocaleString()} / {d.maxTokens.toLocaleString()} tokens</p>
      )}
    </div>
  )
}
