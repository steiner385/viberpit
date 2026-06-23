/**
 * SubSessionView — read-only drill-down into a single sub-agent's transcript.
 *
 * The sub-agent's turns live in the parent session's runtime at
 * `rt.state.subAgents[agentId].turns` (populated live as the sub-agent streams).
 * We render them read-only with a breadcrumb back to the parent session.
 */
import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { TranscriptView } from './TranscriptView'

interface Props {
  sessionId: string
  agentId: string
  onBack: () => void
}

export function SubSessionView({ sessionId, agentId, onBack }: Props) {
  const navigate = useNavigate()
  const rt = useSessionRuntime(sessionId)
  const thread = rt.state.subAgents[agentId]
  const turns = thread?.turns ?? []
  const meta = rt.subAgents.find((a) => a.toolId === agentId)
  const label = meta?.type ?? 'sub-agent'

  return (
    <div className="flex flex-col h-screen bg-gray-50 dark:bg-gray-900">
      <header className={cn('flex items-center gap-3 p-3 border-b',
        'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800')}>
        <button type="button" onClick={onBack} aria-label="Back to session"
          className={cn('rounded px-3 py-1.5 text-sm font-medium min-h-[44px]',
            'border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
          ← Back
        </button>
        <nav aria-label="Breadcrumb" className="text-sm text-gray-600 dark:text-gray-300 truncate">
          <button
            type="button"
            onClick={() => navigate(`/session/${sessionId}`)}
            className="opacity-80 hover:underline focus-visible:outline-none focus-visible:underline"
          >
            {rt.title || sessionId.slice(0, 8)}
          </button>
          <span aria-hidden className="mx-1.5 opacity-50">›</span>
          <span className="font-medium">{label}</span>
          {meta?.status === 'running' && (
            <span className="ml-2 text-xs text-amber-600 dark:text-amber-400">running…</span>
          )}
        </nav>
        <span className="ml-auto text-xs text-gray-400 italic">Read-only sub-agent</span>
      </header>
      <main className="flex-1 overflow-hidden flex flex-col">
        {turns.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {meta?.status === 'running'
                ? 'Sub-agent starting…'
                : 'No live sub-agent activity (open the parent session while it runs to stream it).'}
            </p>
          </div>
        ) : (
          <TranscriptView
            turns={turns}
            toolDetailMode={rt.toolDetailMode}
            thinkingCollapsed={rt.thinkingCollapsed}
            onAnswer={() => {/* read-only */}}
          />
        )}
      </main>
    </div>
  )
}
