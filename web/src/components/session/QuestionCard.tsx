import { useState } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from './useCardKeyboard'

type QuestionBlock = Extract<Block, { kind: 'question' }>

type AnswerMode = 'option' | 'freeform' | 'chat'

export type StagedAnswer = { selected: string[]; mode: Exclude<AnswerMode, 'chat'> }

interface Props {
  block: QuestionBlock
  onAnswer: (questionId: string, selected: string[], mode?: AnswerMode) => void
  onChat?: (questionId: string) => void
  armedForChat?: boolean
  readOnly?: boolean
  staged?: StagedAnswer | null
  onStage?: (questionId: string, answer: StagedAnswer) => void
  /**
   * Auto-focus the first option on mount. Defaults to true for a standalone
   * card; QuestionGroup sets it false for off-screen carousel cards so they
   * don't scroll the shared track to themselves and hijack the view.
   */
  autoFocusOptions?: boolean
}

function optionLabel(opt: Record<string, unknown>): string {
  return String(opt.label ?? opt)
}

function optionDescription(opt: Record<string, unknown>): string | null {
  const d = opt.description
  return typeof d === 'string' && d.trim() ? d : null
}

const hatchBtn = cn(
  'rounded px-3 py-2 text-xs font-medium min-h-[44px]',
  'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
  'text-gray-700 dark:text-gray-200 hover:bg-blue-50 dark:hover:bg-blue-900/30',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)

function MultiSelectOptions({
  block, checked, onToggle, onSubmit, showSubmit, autoFocus,
}: {
  block: QuestionBlock
  checked: Set<string>
  onToggle: (label: string) => void
  onSubmit?: () => void
  showSubmit: boolean
  autoFocus: boolean
}) {
  const kb = useCardKeyboard({
    count: block.options.length,
    multiSelect: true,
    autoFocus,
    onToggle: (i) => onToggle(optionLabel(block.options[i])),
    onSubmit: () => onSubmit?.(),
  })
  return (
    <>
      <div className="space-y-1 mb-3" {...kb.containerProps}>
        {block.options.map((opt, i) => {
          const label = optionLabel(opt)
          const isChecked = checked.has(label)
          return (
            <button
              key={i}
              type="button"
              role="checkbox"
              aria-checked={isChecked}
              {...kb.itemProps(i)}
              onClick={() => onToggle(label)}
              className={cn(
                'flex items-start gap-2 w-full text-left rounded px-3 py-2 text-sm',
                'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
                'text-gray-800 dark:text-gray-200',
                'hover:bg-blue-50 dark:hover:bg-blue-900/30',
                isChecked && 'ring-2 ring-blue-400 bg-blue-50 dark:bg-blue-900/30',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                'min-h-[44px]',
              )}
            >
              <span aria-hidden="true" className="inline-block w-4">{isChecked ? '☑' : '☐'}</span>
              <span className="block">
                <span className="block">{label}</span>
                {optionDescription(opt) && (
                  <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                    {optionDescription(opt)}
                  </span>
                )}
              </span>
            </button>
          )
        })}
      </div>
      {showSubmit && (
        <button
          type="button"
          onClick={() => onSubmit?.()}
          className={cn(
            'rounded px-4 py-2 text-sm font-medium',
            'bg-blue-600 text-white hover:bg-blue-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px]',
          )}
        >
          Submit
        </button>
      )}
    </>
  )
}

function SingleSelectOptions({
  block, onPick, selectedLabel, autoFocus,
}: { block: QuestionBlock; onPick: (label: string) => void; selectedLabel?: string; autoFocus: boolean }) {
  const headerId = `q-header-${block.questionId}`
  const kb = useCardKeyboard({
    count: block.options.length,
    autoFocus,
    onActivate: (i) => onPick(optionLabel(block.options[i])),
  })
  return (
    <div role="radiogroup" aria-labelledby={headerId} className="space-y-1" {...kb.containerProps}>
      {block.options.map((opt, i) => {
        const label = optionLabel(opt)
        const checked = selectedLabel !== undefined ? label === selectedLabel : i === kb.activeIndex
        return (
          <button
            key={i}
            type="button"
            role="radio"
            aria-checked={checked}
            {...kb.itemProps(i)}
            onClick={() => onPick(label)}
            className={cn(
              'w-full text-left rounded px-3 py-2 text-sm',
              'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700',
              'text-gray-800 dark:text-gray-200',
              'hover:bg-blue-50 dark:hover:bg-blue-900/30',
              checked && selectedLabel !== undefined && 'ring-2 ring-blue-400 bg-blue-50 dark:bg-blue-900/30',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'min-h-[44px]',
            )}
          >
            <span className="block">{label}</span>
            {optionDescription(opt) && (
              <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                {optionDescription(opt)}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export function QuestionCard({ block, onAnswer, onChat, armedForChat = false, readOnly = false, staged, onStage, autoFocusOptions = true }: Props) {
  const [writing, setWriting] = useState(false)
  const [draft, setDraft] = useState('')
  const [localChecked, setLocalChecked] = useState<Set<string>>(new Set())

  const grouped = typeof onStage === 'function'

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

  const orphanedNotice = block.orphaned && !readOnly && (
    <div
      data-testid="orphaned-question-notice"
      className="mb-2 rounded border border-amber-300 dark:border-amber-700 bg-amber-100/70 dark:bg-amber-900/30 px-2 py-1.5 text-xs text-amber-900 dark:text-amber-200"
    >
      A daemon restart interrupted this question, so the original turn is gone.
      Your choice will be sent as a new message and the session will continue from
      there.
    </div>
  )

  // Read-only — historical/replayed, OR resolved in place once answered. When the
  // user has answered, `block.answered` carries the chosen labels: highlight those
  // options so the card reads as "here's what you picked", anchored where it was asked.
  if (readOnly) {
    const answered = block.answered ?? []
    const picked = new Set(answered)
    // Free-form / chat replies aren't among the offered options — surface them verbatim.
    const optionLabels = new Set(block.options.map((o) => optionLabel(o)))
    const freeform = answered.filter((a) => !optionLabels.has(a))
    return (
      <div role="group" aria-labelledby={headerId} className={containerClass}>
        {heading}
        <ul className="space-y-1">
          {block.options.map((opt) => {
            const label = optionLabel(opt)
            const isPicked = picked.has(label)
            return (
              <li
                key={label}
                aria-checked={isPicked || undefined}
                role={picked.size > 0 ? 'radio' : undefined}
                className={cn(
                  'flex items-start gap-2 rounded px-3 py-2 text-sm border',
                  isPicked
                    ? 'border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-900/30 text-gray-800 dark:text-gray-200 ring-1 ring-blue-300 dark:ring-blue-700'
                    : 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 text-gray-500 dark:text-gray-400',
                )}
              >
                {picked.size > 0 && (
                  <span aria-hidden="true" className="inline-block w-4">{isPicked ? '✓' : ''}</span>
                )}
                <span className="block">
                  <span className="block">{label}</span>
                  {optionDescription(opt) && (
                    <span className={cn('block text-xs mt-0.5',
                      isPicked ? 'text-gray-500 dark:text-gray-400' : 'text-gray-400 dark:text-gray-500')}>
                      {optionDescription(opt)}
                    </span>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
        {freeform.length > 0 && (
          <p className="mt-2 text-sm text-gray-800 dark:text-gray-200">
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Your answer: </span>
            {freeform.join(', ')}
          </p>
        )}
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
              if (grouped) onStage!(block.questionId, { selected: [t], mode: 'freeform' })
              else onAnswer(block.questionId, [t], 'freeform')
            }}
            className={cn(
              'rounded px-4 py-2 text-sm font-medium min-h-[44px]',
              'bg-blue-600 text-white hover:bg-blue-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            {grouped ? 'Set response' : 'Send response'}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setWriting(true)} className={hatchBtn}>
            ✍️ Write a response
          </button>
          {!grouped && (
            <button type="button" onClick={() => onChat?.(block.questionId)} className={hatchBtn}>
              💬 Chat about this
            </button>
          )}
        </div>
      )}
    </div>
  )

  if (!block.multiSelect) {
    return (
      <div role="group" aria-labelledby={headerId} className={containerClass}>
        {heading}
        {orphanedNotice}
        {/* Grouped: selectedLabel highlights the staged option ('' = nothing
            staged yet, so no radio is checked). Uncontrolled (undefined): fall
            back to the roving keyboard highlight (activeIndex) as before. */}
        <SingleSelectOptions
          block={block}
          autoFocus={autoFocusOptions}
          onPick={(label) =>
            grouped
              ? onStage!(block.questionId, { selected: [label], mode: 'option' })
              : onAnswer(block.questionId, [label])
          }
          selectedLabel={grouped ? (staged?.selected[0] ?? '') : undefined}
        />
        {escapeHatches}
      </div>
    )
  }

  const stagedSet = new Set(staged?.selected ?? [])
  const toggleStaged = (label: string) => {
    const next = new Set(stagedSet)
    if (next.has(label)) next.delete(label)
    else next.add(label)
    onStage!(block.questionId, { selected: Array.from(next), mode: 'option' })
  }
  const toggleLocal = (label: string) =>
    setLocalChecked((prev) => {
      const next = new Set(prev)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })

  return (
    <div role="group" aria-labelledby={headerId} className={containerClass}>
      {heading}
      {orphanedNotice}
      <MultiSelectOptions
        block={block}
        autoFocus={autoFocusOptions}
        checked={grouped ? stagedSet : localChecked}
        onToggle={grouped ? toggleStaged : toggleLocal}
        onSubmit={grouped ? undefined : () => onAnswer(block.questionId, Array.from(localChecked))}
        showSubmit={!grouped}
      />
      {escapeHatches}
    </div>
  )
}
