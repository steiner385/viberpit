// The four RICE inputs, in formula order (`server/pm/score.py:RICE_FIELDS`).
// One component for the inbox's edit-then-accept form and the leaf row's score
// dialog, so the two never disagree on ids, labels or parsing.
import type { OverlayEdits } from '@/api/triage'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { scoreInputValue } from './inboxKeys'

// eslint-disable-next-line react-refresh/only-export-components -- shared with ProposalDetail/LeafEditors, not a component
export const SCORE_FIELDS = ['reach', 'impact', 'effort', 'confidence'] as const
export type ScoreField = typeof SCORE_FIELDS[number]

export function ScoreFields({ edits, onChange, firstRef, idPrefix = 'score' }: {
  edits: OverlayEdits
  onChange: (next: (s: OverlayEdits) => OverlayEdits) => void
  /** The first input, for callers that focus it on open. */
  firstRef?: React.RefObject<HTMLInputElement | null>
  idPrefix?: string
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {SCORE_FIELDS.map((f, i) => (
        <Field key={f} label={f} htmlFor={`${idPrefix}-${f}`}>
          <Input
            id={`${idPrefix}-${f}`}
            type="number"
            aria-label={f}
            ref={i === 0 ? firstRef : undefined}
            value={edits[f] ?? ''}
            onChange={(e) => onChange((s) => ({ ...s, [f]: scoreInputValue(e.target.value) }))}
          />
        </Field>
      ))}
    </div>
  )
}
