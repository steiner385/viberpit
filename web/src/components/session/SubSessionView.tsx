import { cn } from '@/lib/utils'
import { useSessionStream } from '@/session/useSessionStream'
import { selectTurnsForAgent } from '@/session/selectors'
import { TranscriptView } from './TranscriptView'

interface Props { sessionId: string; agentId: string; parentTitle: string; onBack: () => void }

export function SubSessionView({ sessionId, agentId, parentTitle, onBack }: Props) {
  const { state } = useSessionStream(sessionId)
  const turns = selectTurnsForAgent(state.turns, agentId)
  const meta = state.subAgents[agentId]
  const label = meta ? meta.agentType : 'sub-agent'

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
          <span className="opacity-80">{parentTitle}</span>
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
              {meta?.status === 'running' ? 'Sub-agent starting…' : 'No sub-agent activity recorded.'}
            </p>
          </div>
        ) : (
          <TranscriptView turns={turns} permissionMode="interactive"
            onDecision={() => {/* read-only */}} onAnswer={() => {/* read-only */}} />
        )}
      </main>
    </div>
  )
}
