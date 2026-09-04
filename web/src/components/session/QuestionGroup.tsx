import { useLayoutEffect, useRef, useState } from 'react'
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
const navBtn = cn(
  'flex h-7 w-7 items-center justify-center rounded text-base leading-none',
  'text-gray-600 dark:text-gray-300',
  'hover:bg-amber-100/70 dark:hover:bg-amber-900/30',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
  'disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent',
)

export function QuestionGroup({ questions, onAnswer }: Props) {
  const [answers, setAnswers] = useState<Map<string, StagedAnswer>>(new Map())
  const [active, setActive] = useState(0)
  const [trackH, setTrackH] = useState<number | undefined>(undefined)
  const trackRef = useRef<HTMLDivElement>(null)

  // Start on the first question, and stay there until the user moves. Two things
  // conspire to jump the track to the *last* card on open:
  //   1. Focus theft — every card auto-focuses its first option on mount, and in a
  //      shared track focusing an off-screen card scrolls the track to it (last one
  //      wins), firing onScroll → setActive(lastIndex). Fixed at the source: only
  //      the active card auto-focuses (see `autoFocusOptions` below).
  //   2. Snap re-resolution — `snap-mandatory` forces the container to rest on
  //      *some* snap point and re-picks it on every relayout (trackH application,
  //      ResizeObserver), which can resolve to the last card. We use `snap-proximity`
  //      so at rest it stays at offset 0 and never force-jumps.
  // We also pin scrollLeft to 0 before paint so the initial card is deterministic
  // (scrollLeft directly, not scrollIntoView, which would scroll the chat transcript).
  useLayoutEffect(() => {
    if (trackRef.current) trackRef.current.scrollLeft = 0
  }, [])

  // Size the track to the *active* card so the panel grows/shrinks to the
  // question you're viewing — no dead space under short cards, no internal
  // scroll on tall ones. A ResizeObserver re-measures when a card expands
  // (e.g. "Write a response" opens). jsdom has no ResizeObserver / layout, so
  // offsetHeight is 0 and no height style is applied — leaving tests unaffected.
  useLayoutEffect(() => {
    const card = trackRef.current?.children[active] as HTMLElement | undefined
    if (!card) return
    const measure = () => setTrackH(card.offsetHeight || undefined)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(card)
    return () => ro.disconnect()
  }, [active, questions, answers])

  const stage = (questionId: string, answer: StagedAnswer) =>
    setAnswers((prev) => {
      const next = new Map(prev)
      next.set(questionId, answer)
      return next
    })

  const isAnswered = (q: QuestionBlock) => {
    const a = answers.get(q.questionId)
    return a !== undefined && a.selected.length > 0
  }
  const allAnswered = questions.every(isAnswered)
  const answeredCount = questions.filter(isAnswered).length
  const lastIndex = questions.length - 1

  // Drive the snap track to a given card. Scroll-snap handles swipe/drag on its
  // own; this lets the arrows and dots jump there too (and is a no-op in jsdom,
  // which doesn't implement scrollIntoView).
  const goTo = (i: number) => {
    const clamped = Math.max(0, Math.min(lastIndex, i))
    setActive(clamped)
    const card = trackRef.current?.children[clamped] as HTMLElement | undefined
    if (card && typeof card.scrollIntoView === 'function') {
      card.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
    }
  }

  // Keep the indicator in sync when the user swipes/scrolls the track directly.
  const onScroll = () => {
    const track = trackRef.current
    if (!track || track.clientWidth === 0) return
    const i = Math.round(track.scrollLeft / track.clientWidth)
    setActive(Math.max(0, Math.min(lastIndex, i)))
  }

  // An orphaned group has no live waiter to resolve: the server delivers the
  // answer as the next turn's PROMPT (send_prompt), which flips the session to
  // in_progress and clears the interrupted flags. So only the FIRST per-question
  // frame would land — answers 2..N would find no orphaned interaction, return
  // false and come back as QUESTION_NOT_PENDING, while every card had already
  // been optimistically marked answered. Silent partial delivery.
  //
  // Fix stays entirely on the client and leaves the wire protocol alone
  // (answerQuestion is still questionId + selected[]): send ONE frame carrying
  // every question's answer, each entry labelled "<header>: <label>" so the
  // question→answer mapping survives the flattening. The server renders a
  // multi-entry selection one per line (orphaned_answer_prompt).
  const orphaned = !!questions[0]?.orphaned

  const submitAll = () => {
    if (orphaned) {
      const first = questions[0]
      if (!first) return
      const flattened = questions.flatMap((q) => {
        const a = answers.get(q.questionId)
        return (a?.selected ?? []).map((label) => `${q.header}: ${label}`)
      })
      if (flattened.length > 0) {
        onAnswer(first.questionId, flattened, answers.get(first.questionId)?.mode)
      }
      return
    }
    // Live path — unchanged: one frame per question, each resolving its own future.
    for (const q of questions) {
      const a = answers.get(q.questionId)
      if (a) onAnswer(q.questionId, a.selected, a.mode)
    }
  }

  return (
    <div className="space-y-2">
      {/* Position + progress, with prev/next and clickable dots so the carousel
          is discoverable and keyboard-navigable (not just swipe-only). */}
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-gray-600 dark:text-gray-400" aria-live="polite">
          Question {active + 1} of {questions.length}
          <span aria-hidden="true" className="mx-1.5 text-gray-400 dark:text-gray-600">·</span>
          {answeredCount}/{questions.length} answered
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            className={navBtn}
            aria-label="Previous question"
            disabled={active === 0}
            onClick={() => goTo(active - 1)}
          >
            ‹
          </button>
          {questions.map((q, i) => (
            <button
              key={q.questionId}
              type="button"
              aria-label={`Go to question ${i + 1}`}
              aria-current={i === active ? 'true' : undefined}
              onClick={() => goTo(i)}
              className={cn(
                'h-2 w-2 rounded-full transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                i === active
                  ? 'bg-blue-600'
                  : isAnswered(q)
                    ? 'bg-blue-300 dark:bg-blue-700'
                    : 'bg-gray-300 dark:bg-gray-600',
              )}
            />
          ))}
          <button
            type="button"
            className={navBtn}
            aria-label="Next question"
            disabled={active === lastIndex}
            onClick={() => goTo(active + 1)}
          >
            ›
          </button>
        </div>
      </div>

      {/* Horizontal scroll-snap track: one full-width card per snap point. */}
      <div
        ref={trackRef}
        onScroll={onScroll}
        data-testid="question-carousel"
        style={trackH ? { height: `${trackH}px` } : undefined}
        className={cn(
          'flex items-start snap-x snap-proximity overflow-x-auto overflow-y-hidden scroll-smooth gap-3',
          'transition-[height] duration-200', // panel grows/shrinks to the active card
          '-mx-1 px-1', // keep focus rings from clipping at the track edges
          '[scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        )}
      >
        {questions.map((q, i) => (
          <div key={q.questionId} className="w-full shrink-0 snap-center">
            <QuestionCard
              block={q}
              onAnswer={onAnswer}
              staged={answers.get(q.questionId) ?? null}
              onStage={stage}
              // Only the visible card auto-focuses its options. Off-screen cards
              // that grab focus scroll this shared track to themselves, jumping
              // the carousel to the wrong question on open.
              autoFocusOptions={i === active}
            />
          </div>
        ))}
      </div>

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
