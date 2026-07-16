import type { Turn, Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { QuestionCard } from './QuestionCard'
import { QuestionGroup } from './QuestionGroup'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  turns: Turn[]
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  onChat: (questionId: string) => void
  armedQuestionId: string | null
}

/**
 * All live, open questions belonging to the latest turn that has any. Questions
 * from one AskUserQuestion call share a turn, so this returns them as a group.
 */
function liveGroup(turns: Turn[]): QuestionBlock[] {
  let group: QuestionBlock[] = []
  for (const t of turns) {
    const live = t.blocks.filter(
      (b): b is QuestionBlock => b.kind === 'question' && !!b.live,
    )
    if (live.length > 0) group = live
  }
  return group
}

export function PendingQuestionPanel({ turns, onAnswer, onChat, armedQuestionId }: Props) {
  const group = liveGroup(turns)
  if (group.length === 0) return null
  return (
    <div
      role="region"
      aria-label="Pending question"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-amber-200 dark:border-amber-800',
        'bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2',
        // A tall question (4+ options + heading + escape hatches + Submit) can
        // exceed the column height on a short viewport. The panel is flex-shrink-0
        // (it won't compress) and the transcript above is a *separate* scroller, so
        // without an internal scroller the Submit button falls below the fold with
        // no way to reach it. Cap the height and scroll within; overscroll-contain
        // stops the scroll chaining up into the transcript.
        'max-h-[50vh] overflow-y-auto overscroll-contain',
      )}
      data-testid="pending-question-panel"
    >
      {group.length === 1 ? (
        <QuestionCard
          key={group[0].questionId}
          block={group[0]}
          onAnswer={onAnswer}
          onChat={onChat}
          armedForChat={group[0].questionId === armedQuestionId}
        />
      ) : (
        <QuestionGroup questions={group} onAnswer={onAnswer} />
      )}
    </div>
  )
}
