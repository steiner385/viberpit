// web/src/components/session/CommandResultOverlay.tsx
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import type { Command } from '@/session/events'

interface Props {
  result: { render: 'help' | 'context'; data: unknown } | null
  commands: Command[]
  onClose: () => void
}

export function CommandResultOverlay({ result, commands, onClose }: Props) {
  if (!result) return null
  return (
    <Dialog open onClose={onClose} aria-label="Command result" size="md">
      {result.render === 'help' ? (
        <>
          <h2 className="mb-2 text-sm font-semibold text-ink">Commands</h2>
          <ul className="space-y-1">
            {commands.map((c) => (
              <li key={c.name} className="flex gap-2 text-sm">
                <span className="font-mono text-ink">
                  <span aria-hidden="true">/</span>{c.name}
                </span>
                <span className="flex-1 truncate text-ink-muted">{c.description}</span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <>
          <h2 className="mb-2 text-sm font-semibold text-ink">Context usage</h2>
          <ContextBody data={result.data} />
        </>
      )}
      <Button type="button" variant="secondary" size="lg" onClick={onClose} className="mt-3">
        Close
      </Button>
    </Dialog>
  )
}

function ContextBody({ data }: { data: unknown }) {
  const d = (data ?? {}) as { percentage?: number; totalTokens?: number; maxTokens?: number }
  const pct = typeof d.percentage === 'number' ? d.percentage.toFixed(1) : '—'
  return (
    <div className="text-sm text-ink-muted">
      <p>{pct}% of context used</p>
      {typeof d.totalTokens === 'number' && typeof d.maxTokens === 'number' && (
        <p className="text-ink-muted">{d.totalTokens.toLocaleString()} / {d.maxTokens.toLocaleString()} tokens</p>
      )}
    </div>
  )
}
