// The two inline edits on an issue leaf (roadmap spec §8): "Edit scores" —
// click the score, four labelled fields with the computed score — and
// "Reassign an issue's capability". Both write the ccswitch overlay
// (`PATCH /pm/work-items/{id}/overlay`), immediate; the score reaches the board
// through the reconciler. Each is a small dialog rather than an in-row input so
// the row keeps its one-line shape and the change is a deliberate two-step.
import { useRef, useState } from 'react'
import type { Leaf, CapabilityNode } from '@/api/roadmap'
import type { OverlayEdits } from '@/api/triage'
import { useUpdateOverlay } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'
import { CAPABILITY } from '@/features/products/vocab'
import { ScoreFields, SCORE_FIELDS, type ScoreField } from '../inbox/ScoreFields'
import { riceScore } from '../inbox/rice'

const seedOf = (leaf: Leaf): OverlayEdits => ({
  reach: leaf.reach ?? undefined, impact: leaf.impact ?? undefined,
  effort: leaf.effort ?? undefined, confidence: leaf.confidence ?? undefined,
})

export function LeafScoreButton({ leaf }: { leaf: Leaf }) {
  const update = useUpdateOverlay()
  const [open, setOpen] = useState(false)
  const [edits, setEdits] = useState<OverlayEdits>(() => seedOf(leaf))
  const firstRef = useRef<HTMLInputElement>(null)
  const seed = seedOf(leaf)
  // An emptied box is NOT a change: the overlay PATCH cannot clear a RICE field
  // (it assigns only what it is given), so sending the key would read back
  // unchanged while the dialog closed as if it had saved. Blank means "leave
  // this one alone", which is what the live read-out below shows too.
  const effective = (f: ScoreField) => edits[f] ?? seed[f]
  const changed: OverlayEdits = {}
  for (const f of SCORE_FIELDS) {
    if (edits[f] === undefined) continue
    if (edits[f] !== seed[f]) changed[f] = edits[f]
  }
  const dirty = Object.keys(changed).length > 0
  const preview = riceScore(effective('reach'), effective('impact'), effective('effort'), effective('confidence'))
  const openDialog = () => { setEdits(seedOf(leaf)); setOpen(true) }
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="w-12 shrink-0 justify-end tabular-nums text-ink-muted"
        aria-label={`Edit score for #${leaf.number}`}
        title="Priority score — click to edit"
        onClick={openDialog}
      >
        {leaf.score?.toFixed(1) ?? '—'}
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Score #${leaf.number}`} size="sm" initialFocus={firstRef}>
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (dirty) update.mutate({ itemId: leaf.id, edit: changed }, { onSuccess: () => setOpen(false) }) }}>
          <ScoreFields edits={edits} onChange={setEdits} firstRef={firstRef} idPrefix={`leaf-${leaf.id}`} />
          <p className="text-sm text-ink-muted">
            Score: <span aria-live="polite" className="font-semibold tabular-nums text-ink">{preview ?? '—'}</span>
          </p>
          {update.isError && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(update.error, 'Could not save the score')}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" size="lg" disabled={!dirty || update.isPending} loading={update.isPending}>Save</Button>
          </div>
        </form>
      </Dialog>
    </>
  )
}

export function LeafCapabilityChip({ leaf, capabilities }: {
  leaf: Leaf
  /** The product's flattened tree (`flattenCapabilities`); empty when unknown. */
  capabilities: { node: CapabilityNode; depth: number }[]
}) {
  const update = useUpdateOverlay()
  const [open, setOpen] = useState(false)
  const [target, setTarget] = useState(leaf.featureId ?? '')
  if (capabilities.length === 0) return null
  const current = capabilities.find(({ node }) => node.id === leaf.featureId)?.node
  const dirty = target !== '' && target !== (leaf.featureId ?? '')
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="max-w-40 truncate text-xs text-ink-muted"
        aria-label={`Move #${leaf.number} to another ${CAPABILITY.singular.toLowerCase()}`}
        onClick={() => { setTarget(leaf.featureId ?? ''); setOpen(true) }}
      >
        {current?.title ?? current?.slug ?? `no ${CAPABILITY.singular.toLowerCase()}`}
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Move #${leaf.number}`} size="sm">
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (dirty) update.mutate({ itemId: leaf.id, edit: { featureId: target } }, { onSuccess: () => setOpen(false) }) }}>
          <Field label={CAPABILITY.singular} htmlFor={`leaf-cap-${leaf.id}`}>
            <Select id={`leaf-cap-${leaf.id}`} value={target} onChange={(e) => setTarget(e.target.value)}>
              {/* An unclassified leaf seeds `''`, which matches no option — the
                  select would open on nothing at all. This names that state
                  instead; `dirty` already refuses it as a destination. */}
              <option value="">— {CAPABILITY.singular.toLowerCase()} —</option>
              {capabilities.map(({ node, depth }) => (
                <option key={node.id} value={node.id}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
              ))}
            </Select>
          </Field>
          {update.isError && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(update.error, 'Could not move the issue')}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" size="lg" disabled={!dirty || update.isPending} loading={update.isPending}>Move</Button>
          </div>
        </form>
      </Dialog>
    </>
  )
}
