import { useState } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { QuestionCard, type StagedAnswer } from './QuestionCard'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  questions: QuestionBlock[]
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
}

/**
 * Renders all questions from a single AskUserQuestion tool call together and
 * batches their answers behind one submit. Answers are staged locally so the
 * user can revisit and change any of them before committing; nothing is sent
 * until every question is answered and "Submit all answers" is pressed.
 */
export function QuestionGroup({ questions, onAnswer }: Props) {
  const [answers, setAnswers] = useState<Map<string, StagedAnswer>>(new Map())

  const stage = (questionId: string, answer: StagedAnswer) =>
    setAnswers((prev) => {
      const next = new Map(prev)
      next.set(questionId, answer)
      return next
    })

  const allAnswered = questions.every((q) => {
    const a = answers.get(q.questionId)
    return a !== undefined && a.selected.length > 0
  })

  const submitAll = () => {
    for (const q of questions) {
      const a = answers.get(q.questionId)
      if (a) onAnswer(q.questionId, a.selected, a.mode)
    }
  }

  return (
    <div className="space-y-2">
      {questions.map((q) => (
        <QuestionCard
          key={q.questionId}
          block={q}
          onAnswer={onAnswer}
          staged={answers.get(q.questionId) ?? null}
          onStage={stage}
        />
      ))}
      <button
        type="button"
        disabled={!allAnswered}
        onClick={submitAll}
        className={cn(
          'rounded px-4 py-2 text-sm font-medium min-h-[44px]',
          'bg-blue-600 text-white hover:bg-blue-700',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'disabled:opacity-50 disabled:cursor-not-allowed',
        )}
      >
        Submit all answers
      </button>
    </div>
  )
}
