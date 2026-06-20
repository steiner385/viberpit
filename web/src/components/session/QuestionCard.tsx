import { useState } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'

type QuestionBlock = Extract<Block, { kind: 'question' }>

interface Props {
  block: QuestionBlock
  onAnswer: (questionId: string, selected: string[]) => void
}

function optionLabel(opt: Record<string, unknown>): string {
  return String(opt.label ?? opt)
}

export function QuestionCard({ block, onAnswer }: Props) {
  const [checked, setChecked] = useState<Set<string>>(new Set())

  if (!block.multiSelect) {
    // Single-select: radio-style, auto-submit on select
    return (
      <div
        role="radiogroup"
        aria-labelledby={`q-header-${block.questionId}`}
        className="rounded border border-gray-200 dark:border-gray-700 p-3 my-1"
      >
        <div id={`q-header-${block.questionId}`} className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">
          {block.header}
        </div>
        <p className="mb-3 text-sm text-gray-700 dark:text-gray-300">{block.question}</p>
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
      </div>
    )
  }

  // Multi-select: checkboxes + Submit
  return (
    <div
      role="group"
      aria-labelledby={`q-header-${block.questionId}`}
      className="rounded border border-gray-200 dark:border-gray-700 p-3 my-1"
    >
      <div id={`q-header-${block.questionId}`} className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">
        {block.header}
      </div>
      <p className="mb-3 text-sm text-gray-700 dark:text-gray-300">{block.question}</p>
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
    </div>
  )
}
