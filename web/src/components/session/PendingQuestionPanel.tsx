import type { Turn, Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { QuestionCard } from './QuestionCard'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  turns: Turn[]
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  onChat: (questionId: string) => void
  armedQuestionId: string | null
}

/** First live, open question across all turns (one-at-a-time pinning). */
export function findFirstLiveQuestion(turns: Turn[]): QuestionBlock | null {
  for (const t of turns) {
    for (const b of t.blocks) {
      if (b.kind === 'question' && b.live) return b
    }
  }
  return null
}

export function PendingQuestionPanel({ turns, onAnswer, onChat, armedQuestionId }: Props) {
  const q = findFirstLiveQuestion(turns)
  if (!q) return null
  return (
    <div
      className={cn(
        'flex-shrink-0 border-t border-amber-200 dark:border-amber-800',
        'bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2',
      )}
      data-testid="pending-question-panel"
    >
      <QuestionCard
        block={q}
        onAnswer={onAnswer}
        onChat={onChat}
        armedForChat={q.questionId === armedQuestionId}
      />
    </div>
  )
}
