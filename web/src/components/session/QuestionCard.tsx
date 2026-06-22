import { useState } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'

type QuestionBlock = Extract<Block, { kind: 'question' }>

type AnswerMode = 'option' | 'freeform' | 'chat'

interface Props {
  block: QuestionBlock
  onAnswer: (questionId: string, selected: string[], mode?: AnswerMode) => void
  onChat?: (questionId: string) => void
  armedForChat?: boolean
  readOnly?: boolean
}

function optionLabel(opt: Record<string, unknown>): string {
  return String(opt.label ?? opt)
}

const hatchBtn = cn(
  'rounded px-3 py-2 text-xs font-medium min-h-[44px]',
  'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
  'text-gray-700 dark:text-gray-200 hover:bg-blue-50 dark:hover:bg-blue-900/30',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)

export function QuestionCard({ block, onAnswer, onChat, armedForChat = false, readOnly = false }: Props) {
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [writing, setWriting] = useState(false)
  const [draft, setDraft] = useState('')

  const containerClass = 'rounded border border-gray-200 dark:border-gray-700 p-3 my-1'
  const headerId = `q-header-${block.questionId}`

  const heading = (
    <>
      <div id={headerId} className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">
        {block.header}
      </div>
      <p className="mb-3 text-sm text-gray-700 dark:text-gray-300">{block.question}</p>
    </>
  )

  // Read-only (historical / replayed) — show what was asked, no interaction.
  if (readOnly) {
    return (
      <div role="group" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <ul className="space-y-1">
          {block.options.map((opt) => (
            <li
              key={optionLabel(opt)}
              className={cn(
                'rounded px-3 py-2 text-sm',
                'bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700',
                'text-gray-500 dark:text-gray-400',
              )}
            >
              {optionLabel(opt)}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  // Armed for chat: structured options collapse to a quiet banner; the user
  // types their reply in the main composer (routed back as the answer).
  if (armedForChat) {
    return (
      <div role="group" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <div className="flex items-center justify-between gap-2 rounded bg-blue-50 dark:bg-blue-900/30 px-3 py-2 text-sm text-blue-800 dark:text-blue-200">
          <span>💬 Replying in chat — type your message in the composer below.</span>
          <button type="button" onClick={() => onChat?.(block.questionId)} className={cn(hatchBtn, 'flex-shrink-0')}>
            Cancel
          </button>
        </div>
      </div>
    )
  }

  const escapeHatches = (
    <div className="mt-3 border-t border-gray-100 dark:border-gray-800 pt-3">
      {writing ? (
        <div className="space-y-2">
          <textarea
            aria-label="Write a response"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={2}
            placeholder="Type your own answer…"
            className={cn(
              'w-full resize-none rounded-md border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 text-sm',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 min-h-[44px]',
            )}
          />
          <button
            type="button"
            onClick={() => {
              const t = draft.trim()
              if (!t) return
              onAnswer(block.questionId, [t], 'freeform')
            }}
            className={cn(
              'rounded px-4 py-2 text-sm font-medium min-h-[44px]',
              'bg-blue-600 text-white hover:bg-blue-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            Send response
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setWriting(true)} className={hatchBtn}>
            ✍️ Write a response
          </button>
          <button type="button" onClick={() => onChat?.(block.questionId)} className={hatchBtn}>
            💬 Chat about this
          </button>
        </div>
      )}
    </div>
  )

  if (!block.multiSelect) {
    return (
      <div role="radiogroup" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <div className="space-y-1">
          {block.options.map((opt, i) => {
            const label = optionLabel(opt)
            return (
              <button
                key={i}
                type="button"
                role="radio"
                aria-checked={false}
                onClick={() => onAnswer(block.questionId, [label])}
                className={cn(
                  'w-full text-left rounded px-3 py-2 text-sm',
                  'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                  'text-gray-800 dark:text-gray-200',
                  'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                  'min-h-[44px]',
                )}
              >
                {label}
              </button>
            )
          })}
        </div>
        {escapeHatches}
      </div>
    )
  }

  return (
    <div role="group" aria-labelledby={headerId} className={containerClass}>
      {heading}
      <div className="space-y-1 mb-3">
        {block.options.map((opt, i) => {
          const label = optionLabel(opt)
          return (
            <label
              key={i}
              className={cn(
                'flex items-center gap-2 rounded px-3 py-2 text-sm cursor-pointer',
                'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                'text-gray-800 dark:text-gray-200',
                'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                'min-h-[44px]',
              )}
            >
              <input
                type="checkbox"
                checked={checked.has(label)}
                onChange={(e) => {
                  setChecked((prev) => {
                    const next = new Set(prev)
                    if (e.target.checked) next.add(label)
                    else next.delete(label)
                    return next
                  })
                }}
                className="h-4 w-4 rounded border-gray-300"
              />
              <span>{label}</span>
            </label>
          )
        })}
      </div>
      <button
        type="button"
        onClick={() => onAnswer(block.questionId, Array.from(checked))}
        className={cn(
          'rounded px-4 py-2 text-sm font-medium',
          'bg-blue-600 text-white hover:bg-blue-700',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'min-h-[44px]',
        )}
      >
        Submit
      </button>
      {escapeHatches}
    </div>
  )
}
